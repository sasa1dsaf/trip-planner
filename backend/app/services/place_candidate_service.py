"""为动态城市规划采集并校验景点、餐饮和住宿 POI 候选。"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

import httpx

from app.services.city_registry_service import normalize_city_name
from app.services.map_service import AmapServiceError, search_places


logger = logging.getLogger(__name__)


class CandidateCollectionUnavailableError(RuntimeError):
    """地图服务不可用，无法完成动态候选采集。"""

    def __init__(
        self,
        message: str,
        reason: str | None = None,
        category: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.category = category


class PlaceCandidateCategory(StrEnum):
    """动态规划当前支持的候选类别。"""

    SPOT = "spot"
    MEAL = "meal"
    HOTEL = "hotel"


@dataclass(frozen=True)
class PlaceCandidate:
    """可进入动态 Planner 候选池的地图实体。"""

    poi_id: str
    name: str
    category: PlaceCandidateCategory
    address: str | None
    city: str | None
    district: str | None
    type_name: str | None
    latitude: float
    longitude: float
    image_url: str | None
    average_cost: float | None = None
    source_type: str = "amap_poi"


@dataclass
class CityCandidatePool:
    """指定城市的三类候选以及覆盖校验结果。"""

    city: str
    adcode: str | None
    administrative_level: str | None = None
    candidates: dict[PlaceCandidateCategory, list[PlaceCandidate]] = field(
        default_factory=dict
    )
    minimum_counts: dict[PlaceCandidateCategory, int] = field(default_factory=dict)

    def candidates_for(
        self,
        category: PlaceCandidateCategory,
    ) -> list[PlaceCandidate]:
        return self.candidates.get(category, [])

    @property
    def shortages(self) -> dict[PlaceCandidateCategory, int]:
        return {
            category: minimum - len(self.candidates_for(category))
            for category, minimum in self.minimum_counts.items()
            if len(self.candidates_for(category)) < minimum
        }

    @property
    def meets_minimum(self) -> bool:
        return not self.shortages


DEFAULT_MINIMUM_COUNTS: dict[PlaceCandidateCategory, int] = {
    PlaceCandidateCategory.SPOT: 8,
    PlaceCandidateCategory.MEAL: 12,
    PlaceCandidateCategory.HOTEL: 8,
}

_CATEGORY_SEARCHES: dict[PlaceCandidateCategory, tuple[str, str]] = {
    PlaceCandidateCategory.SPOT: ("景点", "风景名胜"),
    PlaceCandidateCategory.MEAL: ("美食", "餐饮服务"),
    PlaceCandidateCategory.HOTEL: ("酒店", "住宿服务"),
}

_CATEGORY_LABELS: dict[PlaceCandidateCategory, str] = {
    PlaceCandidateCategory.SPOT: "景点",
    PlaceCandidateCategory.MEAL: "餐饮",
    PlaceCandidateCategory.HOTEL: "住宿",
}

_REQUEST_SPLIT_PATTERN = re.compile(r"[\s,，。；;、/|]+")
_REQUEST_PREFIX_PATTERN = re.compile(
    r"^(?:想去|要去|去|到|想|要|希望|计划|安排|参观|游览|游玩|打卡|看看|逛一逛|逛|体验)+"
)
_REQUEST_SUFFIX_PATTERN = re.compile(
    r"(?:一日游|半日游|一天|两天|三天|上午|下午|晚上|附近|周边|等)+$"
)
_SPOT_NAME_HINTS = (
    "楼",
    "湖",
    "山",
    "寺",
    "塔",
    "园",
    "馆",
    "院",
    "城",
    "街",
    "巷",
    "桥",
    "宫",
    "陵",
    "峰",
    "岛",
    "谷",
    "洞",
    "泉",
    "瀑布",
    "古镇",
    "古城",
    "大学",
    "公园",
    "景区",
    "广场",
    "纪念",
    "博物",
    "美术",
    "游乐",
    "度假",
    "长城",
    "水镇",
    "乐园",
    "湿地",
    "码头",
)
_GENERIC_PREFERENCE_WORDS = {
    "自然风景",
    "拍照",
    "美食",
    "小吃",
    "休闲",
    "摄影",
    "出片",
    "亲子",
    "浪漫",
    "购物",
    "夜景",
    "博物馆",
    "公园",
    "古镇",
    "古城",
    "度假",
    "游乐",
    "温泉",
    "演出",
}


def _normalize_match_text(value: str) -> str:
    """去掉空白和常见标点，便于比较用户点名与高德 POI 名称。"""
    return re.sub(r"[\s()（）·\-—_]+", "", value).lower()


def _clean_requested_spot_token(token: str, city: str) -> list[str]:
    """从备注片段中提取可能的景点名称，并生成去掉城市名的变体。"""
    cleaned = _REQUEST_PREFIX_PATTERN.sub("", token.strip())
    cleaned = _REQUEST_SUFFIX_PATTERN.sub("", cleaned).strip()
    without_city = _REQUEST_PREFIX_PATTERN.sub("", cleaned.replace(city, "")).strip()

    variants: list[str] = []
    for candidate in (cleaned, without_city):
        if not candidate or candidate in _GENERIC_PREFERENCE_WORDS:
            continue
        if not 2 <= len(candidate) <= 20:
            continue
        if not any(hint in candidate for hint in _SPOT_NAME_HINTS):
            continue
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _extract_requested_spot_queries(
    special_notes: str | None,
    preferences: list[str] | None,
    city: str,
) -> list[str]:
    """从用户备注和偏好中提取明确点名的景点查询词。"""
    text_parts = [part for part in (special_notes, "、".join(preferences or [])) if part]
    queries: list[str] = []
    for part in text_parts:
        for token in _REQUEST_SPLIT_PATTERN.split(part):
            for query in _clean_requested_spot_token(token, city):
                if query not in queries:
                    queries.append(query)
    return queries[:6]


def _search_requested_spot_candidates(
    queries: list[str],
    city: str,
    adcode: str | None,
    administrative_level: str | None,
) -> list[PlaceCandidate]:
    """逐个按名称查询高德并转成候选；查不到时静默跳过。"""
    candidates: list[PlaceCandidate] = []
    seen_ids: set[str] = set()

    for query in queries:
        normalized_query = _normalize_match_text(query)
        if any(
            normalized_query in _normalize_match_text(candidate.name)
            or _normalize_match_text(candidate.name) in normalized_query
            for candidate in candidates
        ):
            continue

        places: list[dict[str, object]] = []
        for attempt in range(2):
            try:
                places = search_places(
                    keyword=query,
                    city=adcode or normalize_city_name(city),
                    page_size=5,
                    city_limit=True,
                )
                break
            except AmapServiceError as exc:
                if exc.reason == "10021" and attempt == 0:
                    time.sleep(0.5)
                    continue
                break
            except (httpx.HTTPError, RuntimeError):
                break

        for place in places:
            candidate = _to_candidate(
                place,
                city=city,
                category=PlaceCandidateCategory.SPOT,
                adcode=adcode,
                administrative_level=administrative_level,
            )
            if candidate is None:
                continue
            normalized_name = _normalize_match_text(candidate.name)
            if (
                normalized_query not in normalized_name
                and normalized_name not in normalized_query
            ):
                continue
            if candidate.poi_id in seen_ids:
                continue
            seen_ids.add(candidate.poi_id)
            candidates.append(candidate)
            break

    return candidates


def _place_text(value: object) -> str:
    if value is None or isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def _valid_adcode(value: object) -> str | None:
    adcode = _place_text(value)
    return adcode if len(adcode) == 6 and adcode.isdigit() else None


def _belongs_to_administrative_area(
    place_adcode: str,
    target_adcode: str,
    administrative_level: str | None,
) -> bool:
    """按行政区层级比较 POI adcode，支持地级市和区县级目的地。"""
    level = administrative_level
    if level is None:
        if target_adcode.endswith("0000"):
            level = "province"
        elif target_adcode.endswith("00"):
            level = "city"
        else:
            level = "district"

    if level == "province":
        return place_adcode[:2] == target_adcode[:2]
    if level == "city":
        return place_adcode[:4] == target_adcode[:4]
    if level == "district":
        return place_adcode == target_adcode
    return False


def _belongs_to_city_name(place: Mapping[str, object], city: str) -> bool:
    """在 adcode 缺失时，用城市名做保守的范围校验。"""
    place_city = _place_text(place.get("cityname"))
    if not place_city:
        return True

    normalized_city = normalize_city_name(city)
    normalized_place_city = normalize_city_name(place_city)
    return (
        normalized_city == normalized_place_city
        or normalized_city in normalized_place_city
        or normalized_place_city in normalized_city
    )


def _belongs_to_target_area(
    place: Mapping[str, object],
    city: str,
    adcode: str | None,
    administrative_level: str | None,
) -> bool:
    target_adcode = _valid_adcode(adcode)
    place_adcode = _valid_adcode(place.get("adcode"))
    if target_adcode is not None and place_adcode is not None:
        return _belongs_to_administrative_area(
            place_adcode,
            target_adcode,
            administrative_level,
        )
    return _belongs_to_city_name(place, city)


def _to_candidate(
    place: Mapping[str, object],
    city: str,
    category: PlaceCandidateCategory,
    adcode: str | None = None,
    administrative_level: str | None = None,
) -> PlaceCandidate | None:
    poi_id = _place_text(place.get("poi_id"))
    name = _place_text(place.get("name"))
    latitude = place.get("latitude")
    longitude = place.get("longitude")

    if not poi_id or not name or not _belongs_to_target_area(
        place,
        city=city,
        adcode=adcode,
        administrative_level=administrative_level,
    ):
        return None
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None

    raw_average_cost = place.get("average_cost")
    average_cost = (
        float(raw_average_cost)
        if isinstance(raw_average_cost, (int, float)) and raw_average_cost > 0
        else None
    )

    return PlaceCandidate(
        poi_id=poi_id,
        name=name,
        category=category,
        address=_place_text(place.get("address")) or None,
        city=_place_text(place.get("cityname")) or None,
        district=_place_text(place.get("adname")) or None,
        type_name=_place_text(place.get("type")) or None,
        latitude=float(latitude),
        longitude=float(longitude),
        image_url=_place_text(place.get("image_url")) or None,
        average_cost=average_cost,
    )


def _filter_candidates(
    places: list[dict[str, object]],
    city: str,
    category: PlaceCandidateCategory,
    adcode: str | None = None,
    administrative_level: str | None = None,
) -> list[PlaceCandidate]:
    candidates: list[PlaceCandidate] = []
    seen_keys: set[str] = set()

    for place in places:
        candidate = _to_candidate(
            place,
            city=city,
            category=category,
            adcode=adcode,
            administrative_level=administrative_level,
        )
        if candidate is None:
            continue

        dedupe_key = candidate.poi_id or (
            f"{candidate.name}:{candidate.longitude:.6f}:{candidate.latitude:.6f}"
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        candidates.append(candidate)

    return candidates


def collect_city_candidate_pool(
    city: str,
    adcode: str | None = None,
    administrative_level: str | None = None,
    page_size: int = 25,
    minimum_counts: Mapping[PlaceCandidateCategory, int] | None = None,
    special_notes: str | None = None,
    preferences: list[str] | None = None,
) -> CityCandidatePool:
    """采集城市三类候选，并计算是否达到动态规划最低覆盖。"""
    normalized_city = normalize_city_name(city)
    resolved_minimums = dict(minimum_counts or DEFAULT_MINIMUM_COUNTS)
    candidates: dict[PlaceCandidateCategory, list[PlaceCandidate]] = {}

    for category, (keyword, type_name) in _CATEGORY_SEARCHES.items():
        try:
            places = search_places(
                keyword=keyword,
                city=adcode or normalized_city,
                page_size=page_size,
                types=type_name,
                city_limit=True,
            )
        except AmapServiceError as exc:
            logger.warning(
                "candidate collection failed: city=%s category=%s reason=%s message=%s",
                normalized_city,
                category.value,
                exc.reason or "amap_error",
                str(exc),
            )
            raise CandidateCollectionUnavailableError(
                f"暂时无法获取“{normalized_city}”的{_CATEGORY_LABELS[category]}候选，请稍后重试。",
                reason=exc.reason or "amap_error",
                category=category.value,
            ) from exc
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning(
                "candidate collection failed: city=%s category=%s reason=map_service_unavailable",
                normalized_city,
                category.value,
            )
            raise CandidateCollectionUnavailableError(
                f"暂时无法获取“{normalized_city}”的{_CATEGORY_LABELS[category]}候选，请稍后重试。",
                reason="map_service_unavailable",
                category=category.value,
            ) from exc
        candidates[category] = _filter_candidates(
            places,
            city=normalized_city,
            category=category,
            adcode=adcode,
            administrative_level=administrative_level,
        )

    requested_queries = _extract_requested_spot_queries(
        special_notes,
        preferences,
        normalized_city,
    )
    requested_spots = _search_requested_spot_candidates(
        requested_queries,
        city=normalized_city,
        adcode=adcode,
        administrative_level=administrative_level,
    )
    if requested_spots:
        requested_ids = {candidate.poi_id for candidate in requested_spots}
        candidates[PlaceCandidateCategory.SPOT] = requested_spots + [
            candidate
            for candidate in candidates[PlaceCandidateCategory.SPOT]
            if candidate.poi_id not in requested_ids
        ]

    return CityCandidatePool(
        city=normalized_city,
        adcode=adcode,
        administrative_level=administrative_level,
        candidates=candidates,
        minimum_counts=resolved_minimums,
    )
