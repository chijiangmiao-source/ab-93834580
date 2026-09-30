"""单次验收组件（compose verify 服务）。

按顺序执行并以退出码报告：

1. 引擎复核：递归协归兼容、必需字段缺失、额外发送变体、递归深层违约、
   无保护别名环、未定义引用等关键结论必须与预期一致；
2. 代码测试：pytest 全量；
3. 构建检查：所有包字节码可编译、应用可导入；
4. 审计接口冒烟：健康检查、提交/重传/冲突 409/重开/页面，全部走真实 HTTP。

用法：
    AUDIT_BASE_URL=http://web:8000 python verify.py
不提供 AUDIT_BASE_URL 时（本地执行），冒烟改为通过 ASGI TestClient 发起真实请求。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from typing import Any

FAILURES: list[str] = []


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        FAILURES.append(name)
    return ok


# ---------------------------------------------------------------------------
# 1. 引擎复核
# ---------------------------------------------------------------------------

INT = {"kind": "int"}
BOOL = {"kind": "bool"}
TEXT = {"kind": "text"}


def rec(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "record", "fields": fields}


def fld(name: str, t: dict[str, Any], required: bool = True) -> dict[str, Any]:
    return {"name": name, "type": t, "required": required}


def var_(tags: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return {"kind": "variant", "tags": [{"label": l, "type": t} for l, t in tags]}


def ref(name: str) -> dict[str, Any]:
    return {"kind": "ref", "name": name}


def review_engine() -> None:
    step("1/4 引擎复核（协归子类型）")
    from app.core import run_audit

    # 1.1 自递归记录协归兼容，且报告递归复用对。
    s_list = [{"name": "List", "type": rec([
        fld("head", INT), fld("tail", ref("List"), required=False)])}]
    r_list = [{"name": "L", "type": rec([
        fld("head", INT), fld("tail", ref("L"), required=False)])}]
    r = run_audit({"sender": s_list, "receiver": r_list})
    check("自递归记录协归兼容", r["conclusion"] == "compatible")
    check("递归处标出复用比较对", len(r["reused_pairs"]) >= 1,
          json.dumps(r.get("reused_pairs"), ensure_ascii=False))

    # 1.2 递归深层基本类型违约必须被发现（不得因协归闭合而吞掉）。
    r = run_audit({
        "sender": [{"name": "N", "type": rec([fld("v", INT), fld("n", ref("N"), required=False)])}],
        "receiver": [{"name": "M", "type": rec([fld("v", TEXT), fld("n", ref("M"), required=False)])}],
    })
    check("递归深层 int/text 违约被裁决",
          r["conclusion"] == "incompatible" and r["violation"]["code"] == "primitive_mismatch")

    # 1.3 必需字段缺失。
    r = run_audit({
        "sender": [{"name": "S", "type": rec([fld("a", INT)])}],
        "receiver": [{"name": "R", "type": rec([fld("a", INT), fld("b", BOOL)])}],
    })
    check("缺失必需字段拒收",
          r["conclusion"] == "incompatible"
          and r["violation"]["code"] == "missing_required_field"
          and r["violation"]["path"][-1] == "字段 b")

    # 1.4 发送端额外变体标签拒收；接收端额外标签合法。
    r = run_audit({
        "sender": [{"name": "S", "type": var_([("A", INT), ("B", BOOL)])}],
        "receiver": [{"name": "R", "type": var_([("A", INT)])}],
    })
    check("额外发送变体标签拒收",
          r["conclusion"] == "incompatible"
          and r["violation"]["code"] == "extra_variant_tag"
          and r["violation"]["path"][-1] == "变体标签 B")

    r = run_audit({
        "sender": [{"name": "S", "type": var_([("A", INT)])}],
        "receiver": [{"name": "R", "type": var_([("A", INT), ("B", BOOL)])}],
    })
    check("接收端额外标签被接受", r["conclusion"] == "compatible")

    # 1.5 宽度子类型：发送端多字段合法；可选字段缺失合法。
    r = run_audit({
        "sender": [{"name": "S", "type": rec([fld("a", INT), fld("x", TEXT)])}],
        "receiver": [{"name": "R", "type": rec([fld("a", INT), fld("o", BOOL, required=False)])}],
    })
    check("额外发送字段与缺失可选字段合法", r["conclusion"] == "compatible")

    # 1.6 无保护别名环与未定义引用一次性反馈。
    r = run_audit({
        "sender": [{"name": "A", "type": ref("B")}, {"name": "B", "type": ref("A")}],
        "receiver": [{"name": "R", "type": ref("Missing")}],
    })
    codes = {e["code"] for e in r["errors"]}
    check("无保护别名环上报", "unguarded_alias_cycle" in codes)
    check("未定义引用上报", "undefined_ref" in codes)
    check("静态问题一次全部反馈", r["conclusion"] == "invalid" and len(r["errors"]) >= 2)

    # 1.7 互递归协归兼容。
    r = run_audit({
        "sender": [
            {"name": "A", "type": var_([("go", ref("B"))])},
            {"name": "B", "type": rec([fld("back", ref("A"), required=False)])},
        ],
        "receiver": [
            {"name": "A2", "type": var_([("go", ref("B2"))])},
            {"name": "B2", "type": rec([fld("back", ref("A2"), required=False)])},
        ],
    })
    check("互递归结构协归兼容", r["conclusion"] == "compatible")


# ---------------------------------------------------------------------------
# 2/3. 测试与构建检查
# ---------------------------------------------------------------------------


def run_pytest() -> None:
    step("2/4 代码测试（pytest）")
    proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=os.getcwd())
    check("pytest 全部通过", proc.returncode == 0, f"exit={proc.returncode}")


def run_build_check() -> None:
    step("3/4 构建检查（字节码编译 + 应用导入）")
    proc = subprocess.run([sys.executable, "-m", "compileall", "-q", "app", "verify.py"])
    check("全部模块字节码编译通过", proc.returncode == 0)
    try:
        import app.main  # noqa: F401
        import app.core  # noqa: F401
        import app.storage  # noqa: F401
        check("应用模块可导入", True)
    except Exception:  # noqa: BLE001
        check("应用模块可导入", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# 4. 真实 HTTP 冒烟
# ---------------------------------------------------------------------------


def _client_class():
    """优先真实 HTTP；未配置服务地址时退回 ASGI TestClient（仍是完整请求链路）。"""
    base = os.environ.get("AUDIT_BASE_URL")
    if base:
        import httpx

        return httpx.Client(base_url=base, timeout=10), True
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.storage import FrozenStore

    main_mod._store = FrozenStore("/tmp/verify-smoke-audits.json")
    if os.path.exists("/tmp/verify-smoke-audits.json"):
        os.remove("/tmp/verify-smoke-audits.json")
    main_mod._store = FrozenStore("/tmp/verify-smoke-audits.json")
    return TestClient(app=main_mod.app), False


def smoke_http() -> None:
    step("4/4 审计接口冒烟（真实请求）")
    client, is_http = _client_class()
    transport = "HTTP " + os.environ.get("AUDIT_BASE_URL", "(ASGI TestClient)")
    print(f"传输方式: {transport}", flush=True)

    try:
        resp = client.get("/healthz")
        check("GET /healthz 200", resp.status_code == 200, resp.text)
        check("健康状态 ok", resp.json().get("status") == "ok")

        page = client.get("/")
        check("GET / 页面 200", page.status_code == 200 and "text/html" in page.headers.get("content-type", ""))

        audit_id = "verify.smoke.recursive.v1"
        payload = {
            "audit_id": audit_id,
            "sender": [{"name": "Node", "type": rec([
                fld("v", INT), fld("next", ref("Node"), required=False)])}],
            "receiver": [{"name": "N", "type": rec([
                fld("v", INT), fld("next", ref("N"), required=False)])}],
        }
        r1 = client.post("/api/audits", json=payload)
        check("递归契约提交 201 且兼容",
              r1.status_code == 201 and r1.json().get("conclusion") == "compatible", r1.text)
        check("提交结果含递归复用对", len(r1.json().get("reused_pairs", [])) >= 1)

        r2 = client.post("/api/audits", json=payload)
        check("相同审计标识相同载荷读取冻结结论 200",
              r2.status_code == 200 and r2.json().get("reused_frozen") is True, r2.text)
        check("冻结结论时间戳一致",
              r1.json().get("created_at") == r2.json().get("created_at"))

        changed = dict(payload)
        changed["receiver"] = [{"name": "N", "type": rec([
            fld("v", BOOL), fld("next", ref("N"), required=False)])}]
        r3 = client.post("/api/audits", json=changed)
        check("契约变化 409 拒绝", r3.status_code == 409
              and r3.json().get("error") == "contract_changed", r3.text)

        r4 = client.get(f"/api/audits/{audit_id}")
        check("重开结论 200 且仍为原冻结契约",
              r4.status_code == 200
              and r4.json()["contract"]["receiver"][0]["type"]["fields"][0]["type"] == INT, r4.text)

        bad = {
            "audit_id": "verify.smoke.invalid",
            "sender": [{"name": "A", "type": ref("Ghost")}],
            "receiver": [{"name": "R", "type": INT}],
        }
        r5 = client.post("/api/audits", json=bad)
        check("非法契约返回 invalid 且不冻结",
              r5.status_code == 200 and r5.json().get("conclusion") == "invalid"
              and client.get("/api/audits/verify.smoke.invalid").status_code == 404, r5.text)

        incompat = {
            "audit_id": "verify.smoke.extra-tag",
            "sender": [{"name": "S", "type": var_([("A", INT), ("Z", INT)])}],
            "receiver": [{"name": "R", "type": var_([("A", INT)])}],
        }
        r6 = client.post("/api/audits", json=incompat)
        check("额外发送变体经 API 判不兼容",
              r6.status_code == 201 and r6.json().get("conclusion") == "incompatible"
              and r6.json()["violation"]["code"] == "extra_variant_tag", r6.text)
    finally:
        if is_http:
            client.close()


def main() -> int:
    print("########## verify：星载递归载荷兼容审计 验收开始 ##########", flush=True)
    try:
        review_engine()
        run_pytest()
        run_build_check()
        smoke_http()
    except Exception:  # noqa: BLE001
        check("verify 自身未异常退出", False, traceback.format_exc())

    step("验收汇总")
    if FAILURES:
        print(f"验收失败：{len(FAILURES)} 项未通过", flush=True)
        for name in FAILURES:
            print(f"  - {name}", flush=True)
        print("RESULT: FAIL", flush=True)
        return 1
    print("全部检查通过：RESULT: PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
