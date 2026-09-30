"""协归引擎与审计流程的测试。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.core import run_audit
from app.main import app
from app.storage import FrozenStore


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

INT = {"kind": "int"}
BOOL = {"kind": "bool"}
TEXT = {"kind": "text"}


def rec(fields):
    return {"kind": "record", "fields": fields}


def fld(name, t, required=True):
    return {"name": name, "type": t, "required": required}


def var(tags):
    return {"kind": "variant", "tags": [{"label": l, "type": t} for l, t in tags]}


def ref(name):
    return {"kind": "ref", "name": name}


def audit(sender, receiver):
    return run_audit({"sender": sender, "receiver": receiver})


# ---------------------------------------------------------------------------
# 递归协归兼容
# ---------------------------------------------------------------------------


def test_self_recursive_records_compatible_coinductively():
    # List = {head: int, tail: List?}
    sender = [
        {"name": "List", "type": rec([
            fld("head", INT),
            fld("tail", ref("List"), required=False),
        ])}
    ]
    receiver = [
        {"name": "L", "type": rec([
            fld("head", INT),
            fld("tail", ref("L"), required=False),
        ])}
    ]
    r = audit(sender, receiver)
    assert r["conclusion"] == "compatible"
    # 递归比较对必须在复用处闭合：根节点对再次出现，路径停在 tail 字段。
    reuse = [m for m in r["reused_pairs"] if m["sender"] == "List" and m["receiver"] == "L"]
    assert reuse and reuse[0]["path"][-1] == "字段 tail"


def test_mutual_recursion_compatible():
    # A -> variant B; B -> record A
    sender = [
        {"name": "A", "type": var([("b", ref("B"))])},
        {"name": "B", "type": rec([fld("a", ref("A"), required=False)])},
    ]
    receiver = [
        {"name": "A2", "type": var([("b", ref("B2"))])},
        {"name": "B2", "type": rec([fld("a", ref("A2"), required=False)])},
    ]
    assert audit(sender, receiver)["conclusion"] == "compatible"


def test_recursive_incompatibility_is_detected_not_looped_forever():
    # 递归内部 int 对 text 不兼容：必须沿递归下降后报告，而不是死循环或误判。
    sender = [{"name": "Node", "type": rec([
        fld("v", INT),
        fld("next", ref("Node"), required=False),
    ])}]
    receiver = [{"name": "N", "type": rec([
        fld("v", TEXT),
        fld("next", ref("N"), required=False),
    ])}]
    r = audit(sender, receiver)
    assert r["conclusion"] == "incompatible"
    assert r["violation"]["code"] == "primitive_mismatch"
    assert "字段 v" in r["violation"]["path"]


def test_reused_pair_must_reuse_same_pair_not_same_node():
    # 同一发送类型对不同接收类型不能被当作已闭合假设。
    sender = [{"name": "Node", "type": rec([
        fld("v", INT),
        fld("next", ref("Node"), required=False),
    ])}]
    receiver = [{"name": "N", "type": rec([
        fld("v", INT),
        fld("next", rec([
            fld("v", INT),
            fld("tail", TEXT, required=True),
        ]), required=False),
    ])}]
    r = audit(sender, receiver)
    # sender Node 对 receiver N 成立，但 Node 对内联记录（要求 tail）缺必需字段。
    assert r["conclusion"] == "incompatible"
    assert r["violation"]["code"] == "missing_required_field"
    assert r["violation"]["path"][-1] == "字段 tail"


# ---------------------------------------------------------------------------
# 字段规则
# ---------------------------------------------------------------------------


def test_missing_required_field():
    sender = [{"name": "S", "type": rec([fld("a", INT)])}]
    receiver = [{"name": "R", "type": rec([fld("a", INT), fld("b", BOOL)])}]
    r = audit(sender, receiver)
    assert r["conclusion"] == "incompatible"
    assert r["violation"]["code"] == "missing_required_field"
    assert r["violation"]["path"] == ["S", "R", "字段 b"]


def test_missing_optional_field_is_fine():
    sender = [{"name": "S", "type": rec([fld("a", INT)])}]
    receiver = [{"name": "R", "type": rec([fld("a", INT), fld("b", BOOL, required=False)])}]
    assert audit(sender, receiver)["conclusion"] == "compatible"


def test_extra_sender_field_is_width_subtype():
    sender = [{"name": "S", "type": rec([fld("a", INT), fld("extra", TEXT)])}]
    receiver = [{"name": "R", "type": rec([fld("a", INT)])}]
    assert audit(sender, receiver)["conclusion"] == "compatible"


def test_optional_field_value_must_still_be_compatible_when_present():
    sender = [{"name": "S", "type": rec([fld("a", INT), fld("b", INT)])}]
    receiver = [{"name": "R", "type": rec([fld("a", INT), fld("b", BOOL, required=False)])}]
    r = audit(sender, receiver)
    assert r["conclusion"] == "incompatible"
    assert r["violation"]["code"] == "primitive_mismatch"
    assert r["violation"]["path"][-1] == "字段 b"


def test_first_violation_stable_by_receiver_field_order():
    # 两个字段都违约，必须稳定选接收端声明顺序的第一个。
    sender = [{"name": "S", "type": rec([fld("a", INT), fld("b", INT)])}]
    receiver = [{"name": "R", "type": rec([fld("a", BOOL), fld("b", BOOL)])}]
    r1 = audit(sender, receiver)
    r2 = audit(sender, receiver)
    assert r1["violation"]["path"] == ["S", "R", "字段 a"]
    assert r1["violation"] == r2["violation"]


def test_missing_field_reported_before_deeper_mismatch():
    sender = [{"name": "S", "type": rec([fld("a", INT), fld("z", INT)])}]
    receiver = [{"name": "R", "type": rec([
        fld("a", BOOL),           # 深层基本类型违约
        fld("missing", TEXT),     # 缺失必需字段
    ])}]
    r = audit(sender, receiver)
    # 字段 a 在前：先裁决 a，直接不兼容。
    assert r["violation"]["path"][-1] == "字段 a"


# ---------------------------------------------------------------------------
# 变体规则
# ---------------------------------------------------------------------------


def test_extra_sender_variant_tag_rejected():
    sender = [{"name": "S", "type": var([("A", INT), ("B", BOOL)])}]
    receiver = [{"name": "R", "type": var([("A", INT)])}]
    r = audit(sender, receiver)
    assert r["conclusion"] == "incompatible"
    assert r["violation"]["code"] == "extra_variant_tag"
    assert r["violation"]["path"][-1] == "变体标签 B"


def test_extra_receiver_tag_allowed():
    sender = [{"name": "S", "type": var([("A", INT)])}]
    receiver = [{"name": "R", "type": var([("A", INT), ("B", BOOL)])}]
    assert audit(sender, receiver)["conclusion"] == "compatible"


def test_shared_tag_payload_mismatch():
    sender = [{"name": "S", "type": var([("A", INT)])}]
    receiver = [{"name": "R", "type": var([("A", BOOL)])}]
    r = audit(sender, receiver)
    assert r["conclusion"] == "incompatible"
    assert r["violation"]["code"] == "primitive_mismatch"
    assert r["violation"]["path"][-1] == "变体标签 A"


def test_record_vs_variant_shape_mismatch():
    sender = [{"name": "S", "type": rec([])}]
    receiver = [{"name": "R", "type": var([("A", INT)])}]
    r = audit(sender, receiver)
    assert r["violation"]["code"] == "shape_mismatch"


def test_bool_is_not_int_nominal_primitives():
    sender = [{"name": "S", "type": BOOL}]
    receiver = [{"name": "R", "type": INT}]
    assert audit(sender, receiver)["conclusion"] == "incompatible"


# ---------------------------------------------------------------------------
# 静态校验：一次性反馈
# ---------------------------------------------------------------------------


def test_undefined_duplicate_bad_identifier_all_reported_at_once(tmp_path=None):
    sender = [
        {"name": "1bad", "type": ref("Missing")},
        {"name": "Dup", "type": INT},
        {"name": "Dup", "type": INT},
        {"name": "S", "type": rec([
            fld("f", INT),
            fld("f", BOOL),
            fld("u", ref("Undefined")),
        ])},
    ]
    receiver = [{"name": "R", "type": INT}]
    r = audit(sender, receiver)
    assert r["conclusion"] == "invalid"
    codes = {e["code"] for e in r["errors"]}
    assert "bad_identifier" in codes
    assert "duplicate_type_name" in codes
    assert "duplicate_field" in codes
    assert "undefined_ref" in codes
    assert len(r["errors"]) >= 4


def test_unguarded_alias_cycle_detected():
    decls = [
        {"name": "A", "type": ref("B")},
        {"name": "B", "type": ref("A")},
    ]
    r = audit(decls, [{"name": "R", "type": ref("A")}])
    assert r["conclusion"] == "invalid"
    assert any(e["code"] == "unguarded_alias_cycle" for e in r["errors"])


def test_self_alias_cycle_detected():
    decls = [{"name": "A", "type": ref("A")}]
    r = audit(decls, decls)
    assert any(e["code"] == "unguarded_alias_cycle" for e in r["errors"])


def test_guarded_cycle_is_not_alias_cycle():
    decls = [{"name": "A", "type": rec([fld("next", ref("A"), required=False)])}]
    r = audit(decls, decls)
    assert r["conclusion"] == "compatible"
    assert not any(e["code"] == "unguarded_alias_cycle" for e in r["errors"])


def test_too_many_types():
    decls = [{"name": f"T{i}", "type": INT} for i in range(25)]
    r = audit(decls, [{"name": "R", "type": INT}])
    assert r["conclusion"] == "invalid"
    assert any(e["code"] == "too_many_types" for e in r["errors"])


def test_duplicate_tag_detected():
    sender = [{"name": "S", "type": {"kind": "variant", "tags": [
        {"label": "A", "type": INT}, {"label": "A", "type": BOOL},
    ]}}]
    r = audit(sender, [{"name": "R", "type": INT}])
    assert any(e["code"] == "duplicate_tag" for e in r["errors"])


def test_empty_sides_rejected():
    r = audit([], [])
    codes = {e["code"] for e in r["errors"]}
    assert codes == {"empty_side"}


# ---------------------------------------------------------------------------
# 冻结存储与 API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DATA_PATH", str(tmp_path / "audits.json"))
    import app.main as main_mod
    main_mod._store = FrozenStore(str(tmp_path / "audits.json"))
    with TestClient(app) as c:
        yield c


SIMPLE_SENDER = [{"name": "S", "type": rec([fld("a", INT)])}]
SIMPLE_RECEIVER = [{"name": "R", "type": rec([fld("a", INT)])}]
BAD_RECEIVER = [{"name": "R", "type": rec([fld("a", BOOL)])}]


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_submit_freeze_resubmit_and_reopen(client):
    payload = {"audit_id": "audit-1", "sender": SIMPLE_SENDER, "receiver": SIMPLE_RECEIVER}
    r1 = client.post("/api/audits", json=payload)
    assert r1.status_code == 201
    assert r1.json()["conclusion"] == "compatible"
    created = r1.json()["created_at"]

    # 相同审计标识 + 相同载荷 → 读取原冻结结论。
    r2 = client.post("/api/audits", json=payload)
    assert r2.status_code == 200
    assert r2.json()["reused_frozen"] is True
    assert r2.json()["created_at"] == created

    # 重开结论。
    r3 = client.get("/api/audits/audit-1")
    assert r3.status_code == 200
    assert r3.json()["conclusion"] == "compatible"
    assert r3.json()["contract"]["sender"] == SIMPLE_SENDER


def test_changed_contract_rejected_and_not_rewritten(client):
    payload = {"audit_id": "audit-2", "sender": SIMPLE_SENDER, "receiver": SIMPLE_RECEIVER}
    r1 = client.post("/api/audits", json=payload)
    assert r1.status_code == 201

    changed = {"audit_id": "audit-2", "sender": SIMPLE_SENDER, "receiver": BAD_RECEIVER}
    r2 = client.post("/api/audits", json=changed)
    assert r2.status_code == 409
    assert r2.json()["error"] == "contract_changed"
    # 冻结的原结论仍是 compatible，且重开得到的仍是旧契约。
    r3 = client.get("/api/audits/audit-2")
    assert r3.json()["conclusion"] == "compatible"
    assert r3.json()["contract"]["receiver"] == SIMPLE_RECEIVER

    # 即使新契约本身非法，冲突同样 409，而不是改写。
    invalid = {"audit_id": "audit-2", "sender": [{"name": "X", "type": ref("Nope")}], "receiver": SIMPLE_RECEIVER}
    r4 = client.post("/api/audits", json=invalid)
    assert r4.status_code == 409


def test_invalid_contract_not_frozen(client):
    payload = {"audit_id": "audit-3",
               "sender": [{"name": "X", "type": ref("Nope")}], "receiver": SIMPLE_RECEIVER}
    r1 = client.post("/api/audits", json=payload)
    assert r1.status_code == 200
    assert r1.json()["conclusion"] == "invalid"
    # 非法结论不应占用审计标识。
    assert client.get("/api/audits/audit-3").status_code == 404


def test_bad_audit_identifier_rejected(client):
    payload = {"audit_id": "bad id!", "sender": SIMPLE_SENDER, "receiver": SIMPLE_RECEIVER}
    r = client.post("/api/audits", json=payload)
    assert r.status_code == 400
    assert r.json()["errors"][0]["code"] == "bad_audit_identifier"


def test_recursive_case_via_api_and_reused_pairs(client):
    sender = [{"name": "Node", "type": rec([
        fld("v", INT), fld("next", ref("Node"), required=False)])}]
    receiver = [{"name": "N", "type": rec([
        fld("v", INT), fld("next", ref("N"), required=False)])}]
    r = client.post("/api/audits", json={"audit_id": "rec-1", "sender": sender, "receiver": receiver})
    assert r.status_code == 201
    body = r.json()
    assert body["conclusion"] == "compatible"
    assert len(body["reused_pairs"]) >= 1


def test_freeze_persists_across_store_instances(tmp_path):
    path = str(tmp_path / "a.json")
    s1 = FrozenStore(path)
    s1.submit("p", SIMPLE_SENDER, SIMPLE_RECEIVER, {"conclusion": "compatible"})
    s2 = FrozenStore(path)
    rec, created, conflict = s2.submit("p", SIMPLE_SENDER, SIMPLE_RECEIVER, {"conclusion": "compatible"})
    assert not created and not conflict
    _, _, conflict2 = s2.submit("p", SIMPLE_SENDER, BAD_RECEIVER, {"conclusion": "incompatible"})
    assert conflict2
    assert s2.get("p")["report"]["conclusion"] == "compatible"
