from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.services.place_candidate_service as candidate_service  # noqa: E402
import app.services.trip_service as trip_service  # noqa: E402
from app.models.schemas import (  # noqa: E402
    BudgetBreakdown,
    DayPlan,
    HotelItem,
    Itinerary,
    MealItem,
    SpotItem,
    TransportItem,
    TripRequest,
)
from app.services import map_service  # noqa: E402
from app.services.place_candidate_service import PlaceCandidateCategory  # noqa: E402


def build_request(travelers: int = 2, budget: float = 10000) -> TripRequest:
    return TripRequest(
        destination="武汉",
        start_date="2026-10-20",
        end_date="2026-10-22",
        travelers=travelers,
        budget=budget,
        preferences=["自然风景", "美食"],
        pace="轻松",
        hotel_level="舒适型",
    )


def build_itinerary(hotel_nightly: float = 0.0) -> Itinerary:
    days = []
    for index in range(1, 4):
        days.append(
            DayPlan(
                day_index=index,
                date=f"2026-10-{19 + index:02d}",
                theme=f"第{index}天",
                spots=[SpotItem(name="黄鹤楼", estimated_cost=60.0)],
                meals=[
                    MealItem(
                        name="武汉热干面",
                        meal_type="午餐",
                        estimated_cost=60.0,
                    )
                ],
                hotel=HotelItem(name="测试酒店", estimated_cost=hotel_nightly),
                transport=[
                    TransportItem(
                        mode="打车",
                        from_place="测试酒店",
                        to_place="黄鹤楼",
                        estimated_cost=0.0,
                    )
                ],
            )
        )
    return Itinerary(
        trip_id="trip_武汉_2026-10-20",
        destination="武汉",
        summary="测试行程",
        days=days,
        budget_breakdown=BudgetBreakdown(),
    )


def _poi(
    poi_id: str,
    name: str,
    type_name: str,
) -> dict[str, object]:
    return {
        "poi_id": poi_id,
        "name": name,
        "type": type_name,
        "adcode": "420100",
        "cityname": "武汉市",
        "adname": "武昌区",
        "address": "武汉市测试路",
        "latitude": 30.5,
        "longitude": 114.3,
        "image_url": None,
        "average_cost": None,
    }


def test_named_spots_from_notes_are_injected_and_prepended(monkeypatch) -> None:
    """备注里点名的东湖、武汉大学即使不在泛搜索前 25 条，也要注入候选池。"""
    generic_spots = [
        _poi(f"spot_{index}", f"普通景点{index}", "风景名胜;风景名胜;风景名胜")
        for index in range(8)
    ]
    meals = [
        _poi(f"meal_{index}", f"餐厅{index}", "餐饮服务;餐厅")
        for index in range(12)
    ]
    hotels = [
        _poi(f"hotel_{index}", f"酒店{index}", "住宿服务;宾馆酒店")
        for index in range(8)
    ]

    def fake_search_places(**kwargs):
        keyword = kwargs["keyword"]
        if keyword == "景点":
            return generic_spots
        if keyword == "美食":
            return meals
        if keyword == "酒店":
            return hotels
        if "黄鹤楼" in keyword:
            return [_poi("huanghelou", "黄鹤楼", "风景名胜;国家级景点")]
        if "东湖" in keyword:
            return [_poi("donghu", "东湖生态旅游风景区", "风景名胜;国家级景点")]
        if "大学" in keyword:
            return [_poi("wuda", "武汉大学", "科教文化服务;学校;高等院校")]
        return []

    monkeypatch.setattr(candidate_service, "search_places", fake_search_places)

    pool = candidate_service.collect_city_candidate_pool(
        city="武汉",
        adcode="420100",
        administrative_level="city",
        special_notes="去武汉参观黄鹤楼、东湖、武汉大学，不想太早起床",
        preferences=["自然风景", "美食"],
    )

    names = [candidate.name for candidate in pool.candidates_for(PlaceCandidateCategory.SPOT)]
    assert names[:3] == ["黄鹤楼", "东湖生态旅游风景区", "武汉大学"]
    assert "普通景点0" in names


def test_missing_named_spot_is_silently_skipped(monkeypatch) -> None:
    """点名景点查不到时保持静默，不影响候选池正常生成。"""

    def fake_search_places(**kwargs):
        keyword = kwargs["keyword"]
        if keyword == "景点":
            return [
                _poi(f"spot_{index}", f"普通景点{index}", "风景名胜;风景名胜")
                for index in range(8)
            ]
        if keyword == "美食":
            return [
                _poi(f"meal_{index}", f"餐厅{index}", "餐饮服务;餐厅")
                for index in range(12)
            ]
        if keyword == "酒店":
            return [
                _poi(f"hotel_{index}", f"酒店{index}", "住宿服务;宾馆酒店")
                for index in range(8)
            ]
        return []

    monkeypatch.setattr(candidate_service, "search_places", fake_search_places)

    pool = candidate_service.collect_city_candidate_pool(
        city="武汉",
        adcode="420100",
        administrative_level="city",
        special_notes="黄鹤楼、不存在的景点",
    )

    names = [candidate.name for candidate in pool.candidates_for(PlaceCandidateCategory.SPOT)]
    assert names == [f"普通景点{index}" for index in range(8)]


def test_hotel_cost_without_amap_price_is_capped_by_level() -> None:
    """高德没有酒店价格时，住宿总额不能无限吃满剩余预算。"""
    itinerary = build_itinerary(hotel_nightly=0.0)
    request = build_request(travelers=2, budget=10000)

    trip_service._allocate_hotel_and_transport_budget(itinerary, request)

    hotel_total = round(
        sum(day.hotel.estimated_cost for day in itinerary.days if day.hotel is not None),
        2,
    )
    assert 0 < hotel_total <= 1200.0  # 舒适型 600/晚/间 × 1 间 × 2 晚


def test_hotel_cost_uses_amap_nightly_price_times_nights() -> None:
    """高德返回酒店单晚价格时，按 单晚价 × 房间数 × 晚数 计算。"""
    itinerary = build_itinerary(hotel_nightly=800.0)
    request = build_request(travelers=2, budget=10000)

    trip_service._allocate_hotel_and_transport_budget(itinerary, request)

    hotel_total = round(
        sum(day.hotel.estimated_cost for day in itinerary.days if day.hotel is not None),
        2,
    )
    assert hotel_total == 1600.0  # 800 × 1 间 × 2 晚


def test_enrich_hotel_uses_amap_average_cost_and_room_count(monkeypatch) -> None:
    """高德酒店人均/单晚价回填时，按每 2 人 1 间换算房间数。"""
    hotel = HotelItem(name="武汉东湖宾馆", estimated_cost=0.0)
    monkeypatch.setattr(
        map_service,
        "_pick_best_place",
        lambda *_args, **_kwargs: {
            "name": "武汉东湖宾馆",
            "address": "武汉市武昌区",
            "latitude": 30.5,
            "longitude": 114.4,
            "poi_id": "hotel_1",
            "image_url": None,
            "average_cost": 500.0,
        },
    )

    map_service._enrich_hotel(hotel, city="武汉", travelers=4)

    assert hotel.estimated_cost == 1000.0  # 500 × 2 间


def test_route_taxi_cost_overrides_distance_formula() -> None:
    """高德返回 taxi_cost 时，交通费优先使用官方参考价。"""
    itinerary = build_itinerary()
    transport = itinerary.days[0].transport[0]
    transport.distance_km = 10.0
    transport.taxi_cost = 42.0
    transport.estimated_cost = 0.0

    trip_service._apply_route_based_transport_costs(itinerary)

    assert transport.estimated_cost == 42.0


def test_enrich_transport_records_taxi_cost(monkeypatch) -> None:
    """路线补全时要把 taxi_cost 一并写回 TransportItem。"""
    transport = TransportItem(mode="打车", from_place="测试酒店", to_place="黄鹤楼")
    monkeypatch.setattr(
        map_service,
        "_geocode_place_text",
        lambda *_args, **_kwargs: {"latitude": 30.5, "longitude": 114.3},
    )
    monkeypatch.setattr(
        map_service,
        "estimate_route",
        lambda **_kwargs: {
            "distance_km": 28.69,
            "estimated_minutes": 68,
            "taxi_cost": 73.0,
        },
    )

    map_service._enrich_transport(transport, city="武汉")

    assert transport.distance_km == 28.69
    assert transport.taxi_cost == 73.0
