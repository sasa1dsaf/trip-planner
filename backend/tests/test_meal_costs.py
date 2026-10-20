from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

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


EMPTY_USAGE = {"prompt_tokens": 0, "completion_tokens": 0}


def build_beijing_request(day_count: int = 1) -> TripRequest:
    """构造一个北京一日游请求，用于餐饮费用测试。"""
    end_day = 20 + day_count - 1
    return TripRequest(
        destination="北京",
        start_date="2026-10-20",
        end_date=f"2026-10-{end_day:02d}",
        travelers=2,
        budget=3200,
        preferences=["美食"],
        pace="轻松",
        hotel_level="舒适型",
    )


def build_itinerary_with_meal_cost(meal_cost: float) -> Itinerary:
    """构造只包含一天的最小 itinerary。"""
    return Itinerary(
        trip_id="trip_北京_2026-10-20",
        destination="北京",
        summary="测试行程",
        days=[
            DayPlan(
                day_index=1,
                date="2026-10-20",
                theme="测试日",
                spots=[SpotItem(name="故宫", estimated_cost=100.0)],
                meals=[
                    MealItem(
                        name="方砖厂69号炸酱面(前门大街店)",
                        meal_type="午餐",
                        estimated_cost=meal_cost,
                    )
                ],
                hotel=HotelItem(name="测试酒店", estimated_cost=0.0),
                transport=[
                    TransportItem(
                        mode="打车",
                        from_place="测试酒店",
                        to_place="故宫",
                        estimated_cost=0.0,
                    )
                ],
            )
        ],
        budget_breakdown=BudgetBreakdown(),
    )


def test_noodle_meal_fallback_uses_per_person_price_and_travelers() -> None:
    """炸酱面类小吃的兜底人均价是 30 元，两人应为 60 元。"""
    cost = trip_service._estimate_meal_cost(
        "方砖厂69号炸酱面(前门大街店)",
        travelers=2,
    )

    assert cost == 60.0


def test_meal_cost_prefers_amap_average_cost_per_person() -> None:
    """高德返回人均消费时，应按 人均 × 人数 计算，优先于兜底价。"""
    cost = trip_service._estimate_meal_cost(
        "方砖厂69号炸酱面(前门大街店)",
        travelers=2,
        average_cost_per_person=45.0,
    )

    assert cost == 90.0


def test_formal_restaurant_fallback_uses_higher_per_person_price() -> None:
    """正餐类餐厅的兜底人均价应高于小吃。"""
    cost = trip_service._estimate_meal_cost("某海景私房菜", travelers=2)

    assert cost == 180.0


def test_parse_average_cost_handles_amap_values() -> None:
    """高德人均消费可能以字符串或数字返回，空值要安全忽略。"""
    assert map_service._parse_average_cost("45.00") == 45.0
    assert map_service._parse_average_cost(30.5) == 30.5
    assert map_service._parse_average_cost("") is None
    assert map_service._parse_average_cost([]) is None
    assert map_service._parse_average_cost(0) is None


def test_allocate_budget_rebalances_hotel_and_transport_without_touching_meals() -> None:
    """餐饮按真实价格计算后，剩余预算应只分配给住宿和交通。"""
    itinerary = build_itinerary_with_meal_cost(meal_cost=60.0)
    request = build_beijing_request()

    trip_service._allocate_hotel_and_transport_budget(itinerary, request)
    trip_service._refresh_budget_breakdown(itinerary, request_budget=request.budget)

    assert itinerary.days[0].meals[0].estimated_cost == 60.0
    assert itinerary.days[0].hotel.estimated_cost > 0
    assert itinerary.days[0].transport[0].estimated_cost > 0
    assert itinerary.budget_breakdown.meals == 60.0
    assert itinerary.budget_breakdown.total < request.budget


def test_generated_noodle_meal_is_priced_per_traveler_not_by_daily_budget(monkeypatch) -> None:
    """生成行程时，两人吃炸酱面不应再显示成几百元一天的餐饮预算。"""
    contexts = [
        (
            "[来源: beijing_guide.md | 标题: 方砖厂69号炸酱面]\n"
            "* **【方砖厂69号炸酱面】招牌菜**：老北京炸酱面。"
            "人均预算 **30元**。"
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

    assert itinerary.days[0].meals[0].name == "方砖厂69号炸酱面"
    assert itinerary.days[0].meals[0].estimated_cost == 60.0
