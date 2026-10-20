from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.services.trip_service as trip_service  # noqa: E402
from app.models.schemas import TripRequest  # noqa: E402


EMPTY_USAGE = {"prompt_tokens": 0, "completion_tokens": 0}


def build_beijing_request() -> TripRequest:
    return TripRequest(
        destination="北京",
        start_date="2026-10-20",
        end_date="2026-10-21",
        travelers=2,
        budget=10000,
        preferences=["自然风景", "拍照", "美食"],
        pace="轻松",
        hotel_level="舒适型",
    )


def test_theme_park_ticket_is_priced_per_person() -> None:
    """环球影城这类主题乐园必须按高票价估算，并乘以出行人数。"""
    cost = trip_service._estimate_ticket_cost("环球影城", travelers=2)

    assert cost == 1000.0


def test_known_attraction_uses_known_per_person_price() -> None:
    """颐和园使用已知人均门票价，而不是默认的 35 元档。"""
    cost = trip_service._estimate_ticket_cost("颐和园", travelers=2)

    assert cost == 60.0


def test_known_free_attraction_costs_nothing() -> None:
    """大理古城这类免费景点应估算为 0，而不是随机门票。"""
    cost = trip_service._estimate_ticket_cost("大理古城", travelers=2)

    assert cost == 0.0


def test_generated_ticket_total_includes_both_travelers(monkeypatch) -> None:
    """生成行程时，门票汇总必须包含两个人的环球影城门票。"""
    contexts = [
        (
            "[来源: beijing_guide.md | 标题: 环球影城]\n"
            "* **位置**：北京市通州区\n"
            "* **简介**：主题乐园，适合全天游玩。"
        )
    ]
    monkeypatch.setattr(
        trip_service,
        "collect_trip_context",
        lambda **_: (contexts, EMPTY_USAGE, EMPTY_USAGE, EMPTY_USAGE),
    )
    monkeypatch.setattr(
        trip_service,
        "generate_planner_draft",
        lambda *_: (None, EMPTY_USAGE),
    )
    monkeypatch.setattr(trip_service, "ENABLE_AMAP_ENRICHMENT", False)

    itinerary = trip_service.generate_trip_itinerary(build_beijing_request())

    assert itinerary.days[0].spots[0].name == "环球影城"
    assert itinerary.days[0].spots[0].estimated_cost == 1000.0
    assert itinerary.budget_breakdown.tickets == 1000.0
