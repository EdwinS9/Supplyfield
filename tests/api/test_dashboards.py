"""The two dashboards: delivery on time per order with its steps, and stock with prices and negotiations."""
from pathlib import Path

from fastapi.testclient import TestClient

from gtl.api import create_app
from gtl.comms import negotiation
from gtl.contracts import Post
from gtl.testing import make_context

WEB = Path(__file__).resolve().parents[2] / "web"
TENANT = {"name": "Harbor Instruments", "value_per_copy": {"deluxe": 10.0, "base": 1.0}}


def fact(board, entity, key, field, value, status="active"):
    return board.append(Post("fact", status, f"{entity}/{key}/{field}",
                             {"entity": entity, "key": key, "field": field, "value": value}, "verifier"))


def forecast(board, status, shortfall, limiting, runs_out):
    return board.append(Post("forecast", status, "forecast", {
        "copies": {}, "shortfall": shortfall, "limiting_part": limiting, "runs_out_at": runs_out,
        "shadow_price": {}}, "planner"))


def seeded():
    ctx = make_context()
    b = ctx.board
    for product, name, demand, packed in (("deluxe", "Deluxe", 100, 40), ("base", "Base", 500, 400)):
        fact(b, "product", product, "name", name)
        fact(b, "product", product, "demand_units", demand)
        fact(b, "product", product, "packed_units", packed)
        fact(b, "product", product, "deadline", "2030-01-02T20:00:00+00:00")
    for product, part in (("deluxe", "insert"), ("deluxe", "card"), ("base", "card")):
        fact(b, "bom", f"{product}|{part}", "qty_per", 1)
    fact(b, "part", "insert", "description", "Printed insert")
    fact(b, "supplier", "s1", "name", "Alpha Print")
    fact(b, "stock", "dc|insert|b1", "qty", 30)
    fact(b, "stock", "dc|insert|b2", "qty", 12, status="quarantined")
    fact(b, "stock", "dc|card|c1", "qty", 900)
    fact(b, "stock", "dc|card|c1", "qty", 950, status="quarantined")  # an earlier booking, corrected
    fact(b, "shipment", "sh1", "part_ids", ["insert"])
    fact(b, "shipment", "sh1", "qty", 10)
    fact(b, "shipment", "sh1", "status", "in_transit")
    fact(b, "supplier_part", "s1|insert", "unit_price", 2.5)
    first = forecast(b, "superseded", {"deluxe": 50, "base": 20}, {"deluxe": "insert", "base": "card"},
                     {"insert": "2030-01-02T09:00:00+00:00"})
    buy = b.append(Post("action", "requested", "accept_offer:o1", {
        "kind": "accept_offer", "requested_by": "resolver", "cost_eur": 100.0,
        "reason": "Accept offer from s1: 50 x insert for EUR 100. Saves 50 copies, cost 100 EUR, net 400 EUR.",
        "params": {"supplier_id": "s1", "part_id": "insert", "qty": 50, "unit_price": 2.0, "currency": "EUR",
                   "offer_key": "s1|o1|1", "copies_saved": {"deluxe": 50, "base": 0}}}, "resolver"))
    b.set_status(buy.id, "needs_approval", "gate", "needs approval by category_buyer")
    b.append(Post("decision", "answered", "Approve", {
        "approver_role": "category_buyer", "approver_person": "Robin", "chosen": 0,
        "options": [{"title": "Buy 50 x insert"}, {"title": "Do not do this"}]}, "gate", depends_on=(buy.id,)))
    b.set_status(buy.id, "approved", "gate")
    b.set_status(buy.id, "executed", "executor")
    b.append(Post("notice", "posted", "n", {"title": "Ordered 50 x insert from s1", "detail": "", "action_id": buy.id,
                                            "rule_id": None}, "executor", depends_on=(buy.id,)))
    b.append(Post("action", "executed", "other", {"kind": "quarantine_batch", "requested_by": "data_steward",
                                                  "reason": "Unrelated.", "params": {"part_id": "bolt"}}, "data_steward"))
    last = forecast(b, "current", {"deluxe": 0, "base": 25}, {"deluxe": None, "base": "card"},
                    {"card": "2030-01-02T15:00:00+00:00"})
    b.append(Post("belief_diff", "posted", "diff", {"trigger": last.id, "changed": [], "summary": ""}, "planner",
                  depends_on=(last.id, first.id)))
    return ctx, TestClient(create_app(b, TENANT, clock=ctx.ports.clock)), buy


def test_products_show_copies_late_before_the_agents_and_now():
    _, api, _ = seeded()
    for path in ("/api/delivery", "/api/procurement"):
        view = api.get(path).json()
        deluxe, base = view["products"]
        assert (deluxe["id"], deluxe["name"], deluxe["premium"]) == ("deluxe", "Deluxe", True) and not base["premium"]
        assert (deluxe["late_before"], deluxe["late_now"], deluxe["recovered"], deluxe["status"]) == (50, 0, 50, "on_time")
        assert (base["late_before"], base["late_now"], base["recovered"], base["status"]) == (20, 25, -5, "late")
        assert "steps" not in deluxe and view["baseline_at"] and view["forecast_at"]
    assert api.get("/api/delivery").json()["tenant_name"] == "Harbor Instruments"


def test_client_orders_are_the_preorders_and_first_day_demand_of_each_product():
    ctx, api, buy = seeded()
    fact(ctx.board, "product", "base", "preorder_units", 420)
    orders = api.get("/api/delivery").json()["orders"]
    assert [o["id"] for o in orders] == ["deluxe:first_day", "base:preorders", "base:first_day"]
    deluxe, pre, rest = orders
    assert (deluxe["label"], deluxe["product"], deluxe["premium"], deluxe["qty"]) == ("All orders", "Deluxe", True, 100)
    assert deluxe["deadline"] == "2030-01-02T20:00:00+00:00"
    assert deluxe["without"] == {"on_time": 50, "late": 50, "ready_at": None}
    assert deluxe["with_agents"]["late"] == 0 and deluxe["change"] == "saved"
    assert [s["post_id"] for s in deluxe["steps"]] == [buy.id]  # the purchase that saved it, in plain bullets
    assert deluxe["steps"][0]["bullets"][0]["text"].startswith("Why: it gets 50 more copies of Deluxe")
    # Pre-orders are served first: 400 of 420 are already packed, and the shortfall falls on the rest.
    assert (pre["label"], pre["qty"]) == ("Pre-orders", 420)
    assert pre["without"]["late"] == 0 and pre["with_agents"]["late"] == 0 and pre["change"] == "on_time"
    assert (rest["label"], rest["qty"]) == ("First-day orders", 80)
    assert (rest["without"]["late"], rest["with_agents"]["late"], rest["change"]) == (20, 25, "worse")
    assert rest["with_agents"]["on_time"] == 55


def test_a_product_no_forecast_covers_is_not_part_of_the_launch():
    ctx, api, _ = seeded()
    fact(ctx.board, "product", "classic", "name", "Classic")
    fact(ctx.board, "product", "classic", "demand_units", 900)
    fact(ctx.board, "product", "classic", "packed_units", 900)
    fact(ctx.board, "product", "classic", "deadline", "2013-09-17")
    for path in ("/api/delivery", "/api/procurement"):
        assert [p["id"] for p in api.get(path).json()["products"]] == ["deluxe", "base"]
    assert {o["product_id"] for o in api.get("/api/delivery").json()["orders"]} == {"deluxe", "base"}


def test_an_order_already_packed_says_so_and_no_forecast_means_no_promise():
    ctx, api, _ = seeded()
    fact(ctx.board, "product", "base", "preorder_units", 300)
    pre = next(o for o in api.get("/api/delivery").json()["orders"] if o["id"] == "base:preorders")
    assert pre["with_agents"] == {"on_time": 300, "late": 0, "ready_at": "packed"} and pre["steps"] == []
    empty = make_context()
    fact(empty.board, "product", "solo", "demand_units", 10)
    [order] = TestClient(create_app(empty.board)).get("/api/delivery").json()["orders"]
    assert order["without"] is None and order["with_agents"] is None and order["change"] == "unknown"


def test_each_open_delivery_shows_its_date_without_and_with_the_agents():
    ctx, api, buy = seeded()
    b = ctx.board
    for field, value in (("part_ids", ["card"]), ("po_numbers", ["po7"]), ("qty", 300), ("status", "arrived_port"),
                         ("eta", "2030-01-01"), ("supplier_id", "s1")):
        fact(b, "shipment", "sh2", field, value)
    fact(b, "shipment", "sh3", "status", "delivered")
    fact(b, "shipment", "sh1", "eta_site", "2030-01-02T06:00:00+00:00")
    fact(b, "shipment", "sh1", "qty", 40, status="quarantined")
    fact(b, "shipment", "sh3", "eta", "2029-12-30")
    fact(b, "shipment", "sh4", "status", "cancelled")
    for field, value in (("supplier_id", "s1"), ("part_id", "card"), ("qty", 70), ("promised_date", "2030-01-03")):
        fact(b, "po_line", "po9|1", field, value)
    fact(b, "po_line", "po7|1", "qty", 300)  # its shipment sh2 already stands for it
    listed = api.get("/api/procurement").json()["deliveries"]
    stuck = {r["id"]: r for r in listed}
    assert set(stuck) == {"o1|1", "sh1", "sh2", "sh3", "po9 / 1"}  # everything on record except the cancelled one
    assert stuck["sh3"]["change"] == "delivered" and stuck["sh3"]["steps"] == []
    line = stuck["po9 / 1"]
    assert (line["type"], line["change"], line["qty"], line["part"], line["supplier"]) == ("po_line", "same", 70, "card", "Alpha Print")
    assert line["without"] == line["with_agents"] == {"at": "2030-01-03", "text": ""}
    # by delivery date, earliest first; no date yet comes last
    assert [r["id"] for r in listed] == ["sh3", "sh1", "po9 / 1", "o1|1", "sh2"]
    assert stuck["sh2"]["change"] == "stuck" and stuck["sh2"]["with_agents"] == stuck["sh2"]["without"]
    assert stuck["sh2"]["without"] == {"at": None, "text": "Waiting at the port, no collection booked"}
    pickup = b.append(Post("action", "needs_approval", "p", {"kind": "request_pickup", "requested_by": "resolver",
                                                              "params": {"shipment_id": "sh2"}}, "resolver"))
    b.append(Post("action", "executed", "d", {"kind": "request_dock_slot", "requested_by": "resolver",
                                               "params": {"shipment_id": "sh2", "when": "2030-01-02T08:00:00+00:00"}},
                  "resolver"))
    waiting = {r["id"]: r for r in api.get("/api/procurement").json()["deliveries"]}["sh2"]
    assert waiting["with_agents"] == {"at": "2030-01-02T08:00:00+00:00", "text": "Collection waits for approval"}
    b.set_status(pickup.id, "executed", "executor")
    rows = {r["id"]: r for r in api.get("/api/procurement").json()["deliveries"]}
    sh2, sh1, bought = rows["sh2"], rows["sh1"], rows["o1|1"]
    assert sh2["with_agents"] == {"at": "2030-01-02T08:00:00+00:00", "text": ""} and sh2["change"] == "unblocked"
    assert sh2["po_numbers"] == ["po7"] and sh2["supplier"] == "Alpha Print" and sh2["used_by"] == ["Base", "Deluxe"]
    assert [s["kind"] for s in sh2["steps"]] == ["request_pickup", "request_dock_slot"]
    assert sh1["without"] == sh1["with_agents"] == {"at": "2030-01-02T06:00:00+00:00", "text": ""}
    assert (sh1["qty"], sh1["qty_before"], sh1["change"], sh1["steps"]) == (10, 40, "corrected", [])
    assert bought["type"] == "purchase" and bought["without"] == {"at": None, "text": "Not ordered"}
    assert bought["change"] == "new" and bought["qty"] == 50 and bought["part"] == "Printed insert"
    assert [s["post_id"] for s in bought["steps"]] == [buy.id]


def test_a_step_reads_as_plain_bullets_why_ruling_people_outcome():
    ctx, api, buy = seeded()
    ctx.board.log_event("explainer", "action.explained", {"outcome": "needs_approval", "rule_id": "r", "written_by": "template",
                                                          "sentences": ["This needed a person.", "s1 is not released."]}, buy.id)
    view = api.get("/api/procurement").json()
    [step] = next(r for r in view["deliveries"] if r["id"] == "o1|1")["steps"]
    assert step["title"] == "Ordered 50 x Printed insert from Alpha Print" and step["status"] == "executed"
    assert step["cost_eur"] == 100 and step["post_id"] == buy.id
    texts = [(b.get("label"), b["text"]) for b in step["bullets"]]
    assert texts == [
        (None, "Why: it gets 50 more copies of Deluxe packed by the deadline, for EUR 100."),
        ("The ruling", "This needed a person. Alpha Print is not released."),
        (None, "Robin (category buyer) approved it."),
        (None, "Carried out."),
    ]
    [other] = view["other_actions"]  # the step that belongs to no delivery is still listed
    assert other["title"] == "Held back batch None of bolt" and other["bullets"][0]["text"] == "Why: Unrelated."


def test_both_screens_are_empty_but_served_before_any_forecast():
    ctx = make_context()
    api = TestClient(create_app(ctx.board))
    view = api.get("/api/procurement").json()
    assert view["products"] == [] and view["deliveries"] == [] and view["other_actions"] == []
    assert view["totals"]["late_now"] is None and view["baseline_at"] is None
    assert api.get("/api/delivery").json()["orders"] == []


def test_stock_counts_usable_held_and_incoming_units_per_part():
    _, api, _ = seeded()
    view = api.get("/api/supply").json()
    stock = {row["part_id"]: row for row in view["stock"]}
    insert, card = stock["insert"], stock["card"]
    assert (insert["name"], insert["usable"], insert["held"], insert["incoming"]) == ("Printed insert", 30, 12, 60)
    assert insert["needed"] == 60 and insert["used_by"] == ["Deluxe"] and insert["runs_out_at"] is None
    assert (card["usable"], card["held"], card["needed"]) == (900, 0, 160)  # the corrected booking is not held stock
    assert card["runs_out_at"] == "2030-01-02T15:00:00+00:00" and view["stock"][0]["part_id"] == "card"


def test_purchases_carry_their_price_next_to_the_listed_price():
    _, api, buy = seeded()
    view = api.get("/api/supply").json()
    [bought] = view["purchases"]
    assert (bought["post_id"], bought["supplier"], bought["part"]) == (buy.id, "Alpha Print", "Printed insert")
    assert (bought["qty"], bought["unit_price"], bought["listed_price"], bought["cost_eur"]) == (50, 2.0, 2.5, 100)
    assert view["totals"]["spend_eur"] == 100


def test_committed_spend_excludes_pending_orders_and_duplicate_executions():
    ctx, api, purchase = seeded()
    ctx.board.append(Post("action", "needs_approval", "next order", purchase.body, "resolver"))
    ctx.board.append(Post("action", "executed", "retry", {**purchase.body, "duplicate_of": purchase.id}, "executor"))
    view = api.get("/api/supply").json()
    assert view["totals"]["spend_eur"] == 100
    assert view["totals"]["committed_orders"] == 1
    assert [row["status"] for row in view["purchases"]] == ["executed", "needs_approval"]


def test_a_negotiation_is_one_thread_from_the_request_to_the_decision():
    ctx, api, _ = seeded()
    b = ctx.board
    question = b.append(Post("question", "asked", "q", {"to_kind": "supplier", "to_id": "s1", "topic": "t"}, "steward"))
    faults = [{"defect_id": "d1", "question_id": question.id, "entity": "delivery_note", "key": "n1",
               "detail": "The note says 5, the order says 8.", "copies_at_risk": 3}]
    negotiation.log(b, "supply_liaison", "asked", [question.id], "s1", ask=1, faults=faults)
    negotiation.log(b, "supply_liaison", "reminded", [question.id], "s1", ask=2, faults=faults)
    assert api.get("/api/supply").json()["totals"]["negotiations_open"] == 1
    terms = [{"entity": "offer", "key": "k", "field": "unit_price", "value": 1.9, "unit": None, "quote": "1.90 each"}]
    negotiation.log(b, "mail_reader", "replied", [question.id], "s1", message_id="m1", terms=terms, faults=faults)
    b.append(negotiation.decision_question({}, "Alpha Print", "s1", "t", "text", (question.id,), "mail_reader"))
    view = api.get("/api/supply").json()
    [talk] = view["negotiations"]
    assert (talk["supplier"], talk["stage"]) == ("Alpha Print", "replied") and talk["terms"] == terms
    assert talk["mistakes"][0]["detail"] == "The note says 5, the order says 8."
    assert [(s["stage"], s["ask"]) for s in talk["steps"]] == [("asked", 1), ("reminded", 2), ("replied", None)]
    assert talk["decision"]["role"] == "buyer" and talk["decision"]["status"] == "waiting"
    assert view["totals"]["negotiations_replied"] == 1 and view["totals"]["negotiations_open"] == 0


def test_the_deliveries_and_procurement_screens_show_steps_inside_the_table():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    for screen in ("delivery", "procurement", "supply"):
        assert f'<a href="#{screen}">' in page and f'<section id="{screen}"' in page
    assert '<section id="delivery">' in page and "<h1>Deliveries</h1>" in page
    assert "<h1>Procurement</h1>" in page
    for block in ("delivery-products", "client-orders", "procurement-products", "procurement-orders", "other-actions"):
        assert f'id="{block}"' in page
    assert '<dialog id="order-steps"' not in page and '<script src="/dashboards.js" defer></script>' in page
    script = (WEB / "dashboards.js").read_text(encoding="utf-8")
    assert '"/api/delivery"' in script and '"/api/procurement"' in script and '"/api/supply"' in script and "ondblclick" in script and "showModal" not in script
    control = (WEB / "control.js").read_text(encoding="utf-8")
    assert '"delivery", "procurement", "supply", "board"' in control
