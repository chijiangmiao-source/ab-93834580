"""递归类型契约的编译、校验与协归结构子类型判定。

载荷类型只允许六种节点：

* ``int`` / ``bool`` / ``text``：基本类型（按名不变）；
* ``record``：记录，字段带 ``required`` 标记；
* ``variant``：带标签变体，每个标签携带一个类型；
* ``ref``：具名引用。

判定方向固定为“发送端是生产者、接收端是消费者”：

* 接收端要求的必需字段必须由发送端提供，可选字段缺失合法；
* 发送端允许携带接收端未要求的额外字段（记录宽度子类型）；
* 发送端出现的每个变体标签必须被接收端声明，载荷逐标签兼容；
* 递归引用以协归关系裁决：比较对在当前证明分支上再次出现时，
  作为余归纳假设直接成立，不展开到固定深度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MAX_TYPES_PER_SIDE = 24

PRIMITIVE_KINDS = ("int", "bool", "text")
STRUCTURAL_KINDS = PRIMITIVE_KINDS + ("record", "variant")


# ---------------------------------------------------------------------------
# 编译产物
# ---------------------------------------------------------------------------


@dataclass
class Node:
    kind: str
    label: str
    # record: [(field, child_id, required)]
    fields: list[tuple[str, int, bool]] = field(default_factory=list)
    # variant: [(label, child_id)]
    tags: list[tuple[str, int]] = field(default_factory=list)
    # ref: 编译期存目标名（str），_resolve_refs 之后变为目标节点 id
    target: int | str | None = None


@dataclass
class CompiledSide:
    side: str
    nodes: list[Node] = field(default_factory=list)
    roots: dict[str, int] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def add_error(self, code: str, location: str, message: str) -> None:
        self.errors.append({"code": code, "where": f"{side_prefix(self.side)}{location}", "message": message})


def side_prefix(side: str) -> str:
    return "发送端" if side == "sender" else "接收端"


@dataclass
class ReusedPair:
    path: list[str]
    sender: str
    receiver: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sender": self.sender, "receiver": self.receiver}


# ---------------------------------------------------------------------------
# 编译与静态校验
# ---------------------------------------------------------------------------


def compile_side(side: str, decls: Any) -> CompiledSide:
    """把一组具名声明编译成节点图，并一次性收集全部静态错误。"""
    cs = CompiledSide(side=side)

    if not isinstance(decls, list):
        cs.add_error("side_not_list", "", "类型声明必须是数组，每个元素为一个具名类型")
        return cs

    if len(decls) > MAX_TYPES_PER_SIDE:
        cs.add_error(
            "too_many_types",
            "",
            f"每套类型声明至多 {MAX_TYPES_PER_SIDE} 个，实际 {len(decls)} 个",
        )
        decls = decls[:MAX_TYPES_PER_SIDE]

    if len(decls) == 0:
        cs.add_error("empty_side", "", "类型声明不能为空，第一个类型作为该侧根类型")

    seen_names: set[str] = set()
    for i, decl in enumerate(decls):
        loc = f"[{i}]"
        if not isinstance(decl, dict):
            cs.add_error("decl_not_object", loc, "类型声明必须是对象 {name, type}")
            continue
        name = decl.get("name")
        if not isinstance(name, str) or not IDENT_RE.match(name or ""):
            cs.add_error("bad_identifier", f"{loc}.name", f"非法类型标识: {name!r}")
            continue
        if name in seen_names:
            cs.add_error("duplicate_type_name", loc, f"重复的类型名称: {name}")
            continue
        seen_names.add(name)
        root_id = _compile_node(cs, decl.get("type"), f"{loc}.type", name)
        cs.roots[name] = root_id
        cs.order.append(name)

    # 解析引用目标并检查未定义引用。
    _resolve_refs(cs)
    _check_unguarded_alias_cycles(cs)
    return cs


def _compile_node(cs: CompiledSide, raw: Any, loc: str, label: str) -> int:
    if not isinstance(raw, dict):
        cs.add_error("node_not_object", loc, "类型节点必须是对象")
        return _append_invalid(cs, label)

    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in STRUCTURAL_KINDS + ("ref",):
        cs.add_error("bad_kind", loc, f"未知类型种类: {kind!r}，允许 int/bool/text/record/variant/ref")
        return _append_invalid(cs, label)

    nid = len(cs.nodes)
    cs.nodes.append(Node(kind=kind, label=label))

    if kind in PRIMITIVE_KINDS:
        return nid

    if kind == "ref":
        target = raw.get("name")
        if not isinstance(target, str) or not IDENT_RE.match(target or ""):
            cs.add_error("bad_identifier", f"{loc}.name", f"非法引用标识: {target!r}")
        cs.nodes[nid].target = target if isinstance(target, str) else None  # 临时：名字
        return nid

    if kind == "record":
        fields = raw.get("fields")
        if not isinstance(fields, list):
            cs.add_error("fields_not_list", f"{loc}.fields", "record 的 fields 必须是数组")
            return nid
        seen_fields: set[str] = set()
        for j, f in enumerate(fields):
            floc = f"{loc}.fields[{j}]"
            if not isinstance(f, dict):
                cs.add_error("field_not_object", floc, "记录字段必须是对象")
                continue
            fname = f.get("name")
            if not isinstance(fname, str) or not IDENT_RE.match(fname or ""):
                cs.add_error("bad_identifier", f"{floc}.name", f"非法字段标识: {fname!r}")
                continue
            if fname in seen_fields:
                cs.add_error("duplicate_field", floc, f"记录内重复字段: {fname}")
                continue
            seen_fields.add(fname)
            required = f.get("required", True)
            if not isinstance(required, bool):
                cs.add_error("bad_required", f"{floc}.required", "required 必须是布尔值")
                required = True
            child = _compile_node(cs, f.get("type"), f"{floc}.type", f"{label}.{fname}")
            cs.nodes[nid].fields.append((fname, child, required))
        return nid

    # variant
    tags = raw.get("tags")
    if not isinstance(tags, list):
        cs.add_error("tags_not_list", f"{loc}.tags", "variant 的 tags 必须是数组")
        return nid
    if len(tags) == 0:
        cs.add_error("empty_variant", loc, "变体至少需要一个标签")
    seen_labels: set[str] = set()
    for j, t in enumerate(tags):
        tloc = f"{loc}.tags[{j}]"
        if not isinstance(t, dict):
            cs.add_error("tag_not_object", tloc, "变体标签必须是对象")
            continue
        lab = t.get("label")
        if not isinstance(lab, str) or not IDENT_RE.match(lab or ""):
            cs.add_error("bad_identifier", f"{tloc}.label", f"非法变体标签: {lab!r}")
            continue
        if lab in seen_labels:
            cs.add_error("duplicate_tag", tloc, f"变体内重复标签: {lab}")
            continue
        seen_labels.add(lab)
        if "type" not in t:
            cs.add_error("tag_without_type", tloc, f"标签 {lab} 必须携带一个类型")
            child = _append_invalid(cs, f"{label}|{lab}")
        else:
            child = _compile_node(cs, t["type"], f"{tloc}.type", f"{label}|{lab}")
        cs.nodes[nid].tags.append((lab, child))
    return nid


def _append_invalid(cs: CompiledSide, label: str) -> int:
    """错误占位节点，避免错误恢复阶段产生二次崩溃。"""
    nid = len(cs.nodes)
    cs.nodes.append(Node(kind="__invalid__", label=label))
    return nid


def _resolve_refs(cs: CompiledSide) -> None:
    for node in cs.nodes:
        if node.kind != "ref":
            continue
        name = node.target  # 编译期临时存放的字符串
        if isinstance(name, str) and name in cs.roots:
            node.target = cs.roots[name]
        else:
            # 未定义引用（bad_identifier 已在名称本身非法时记录过）。
            if isinstance(name, str):
                cs.add_error("undefined_ref", "", f"未定义的类型引用: {name}")
            node.target = None


def _check_unguarded_alias_cycles(cs: CompiledSide) -> None:
    """无保护别名环：只沿“根定义本身即 ref”的边；record/variant 根是保护点，不出边。"""
    root_id_to_name = {root: name for name, root in cs.roots.items()}
    edges: dict[str, str] = {}
    for name in cs.order:
        node = cs.nodes[cs.roots[name]]
        if node.kind == "ref" and isinstance(node.target, int):
            target_name = root_id_to_name.get(node.target)
            if target_name is not None:
                edges[name] = target_name

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in cs.order}
    stack: list[str] = []

    def visit(name: str) -> None:
        color[name] = GRAY
        stack.append(name)
        nxt = edges.get(name)
        if nxt is not None and nxt in color:
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                cs.add_error(
                    "unguarded_alias_cycle",
                    "",
                    "无保护别名环: " + " → ".join(cycle) + "（环路上必须经过 record 或 variant）",
                )
            elif color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[name] = BLACK

    for name in cs.order:
        if color[name] == WHITE:
            visit(name)


# ---------------------------------------------------------------------------
# 协归结构子类型判定
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    code: str
    path: list[str]
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "path_text": " › ".join(self.path), "detail": self.detail}


def check_compatibility(sender: CompiledSide, receiver: CompiledSide) -> dict[str, Any]:
    """裁决发送端根类型是否是接收端根类型的结构子类型。"""
    if not sender.order or not receiver.order:
        # 正常情况下空声明会被静态校验拦截，这里保底。
        return {
            "conclusion": "incompatible",
            "entry": None,
            "violation": Violation(
                "missing_root", [], "发送端或接收端缺少可用的根类型（声明列表的第一个类型为根）"
            ).as_dict(),
            "reused_pairs": [],
            "compared_pairs": 0,
        }

    root_s_name = sender.order[0]
    root_r_name = receiver.order[0]

    state = _CheckState(sender, receiver)
    violation = state.compatible(
        sender.roots[root_s_name],
        receiver.roots[root_r_name],
        [root_s_name, root_r_name],
    )

    report = {
        "conclusion": "incompatible" if violation else "compatible",
        "entry": {"sender": root_s_name, "receiver": root_r_name},
        "violation": violation.as_dict() if violation else None,
        "reused_pairs": [m.as_dict() for m in state.reused],
        "compared_pairs": state.compared,
    }
    return report


class _CheckState:
    def __init__(self, sender: CompiledSide, receiver: CompiledSide) -> None:
        self.sides = {"sender": sender, "receiver": receiver}
        self.active: set[tuple[int, int]] = set()
        self.done: set[tuple[int, int]] = set()
        self.reused: list[ReusedPair] = []
        self.compared = 0

    def _resolve(self, side: str, nid: int) -> int:
        nodes = self.sides[side].nodes
        node = nodes[nid]
        while node.kind == "ref" and isinstance(node.target, int):
            nid = node.target
            node = nodes[nid]
        return nid

    def _label(self, side: str, nid: int) -> str:
        return self.sides[side].nodes[nid].label

    def compatible(self, s_nid: int, r_nid: int, path: list[str]) -> Violation | None:
        s = self._resolve("sender", s_nid)
        r = self._resolve("receiver", r_nid)
        key = (s, r)

        if key in self.done:
            return None
        if key in self.active:
            # 协归假设：同一比较对在当前证明分支上复现，循环在此闭合。
            self.reused.append(
                ReusedPair(path=list(path), sender=self._label("sender", s), receiver=self._label("receiver", r))
            )
            return None

        sn = self.sides["sender"].nodes[s]
        rn = self.sides["receiver"].nodes[r]
        self.compared += 1

        if sn.kind in PRIMITIVE_KINDS or rn.kind in PRIMITIVE_KINDS:
            if sn.kind != rn.kind:
                return Violation(
                    "primitive_mismatch",
                    path,
                    f"基本类型不兼容：发送端 {sn.kind}，接收端要求 {rn.kind}",
                )
            self.done.add(key)
            return None

        if sn.kind != rn.kind:
            return Violation(
                "shape_mismatch",
                path,
                f"结构种类不兼容：发送端 {sn.kind}，接收端要求 {rn.kind}",
            )

        self.active.add(key)
        if sn.kind == "record":
            violation = self._check_record(sn, rn, path)
        else:  # variant
            violation = self._check_variant(sn, rn, path)
        self.active.discard(key)

        if violation is None:
            self.done.add(key)
        return violation

    def _check_record(self, sn: Node, rn: Node, path: list[str]) -> Violation | None:
        send_fields = {name: (cid, req) for name, cid, req in sn.fields}
        # 以接收端字段声明顺序稳定遍历；缺失先于更深层违约被选中。
        for fname, r_child, required in rn.fields:
            fpath = path + [f"字段 {fname}"]
            if fname not in send_fields:
                if required:
                    return Violation(
                        "missing_required_field",
                        fpath,
                        f"接收端要求必需字段 {fname}，发送端未提供",
                    )
                continue
            s_child, _ = send_fields[fname]
            violation = self.compatible(s_child, r_child, fpath)
            if violation:
                return violation
        # 发送端的额外记录字段是合法宽度，不再检查。
        return None

    def _check_variant(self, sn: Node, rn: Node, path: list[str]) -> Violation | None:
        send_tags = {label: cid for label, cid in sn.tags}
        recv_tags = {label: cid for label, cid in rn.tags}

        # 先按接收端标签顺序裁决共有标签的载荷。
        for label, r_child in rn.tags:
            if label in send_tags:
                violation = self.compatible(send_tags[label], r_child, path + [f"变体标签 {label}"])
                if violation:
                    return violation

        # 再按发送端声明顺序找超出接收端的标签。
        for label, _ in sn.tags:
            if label not in recv_tags:
                return Violation(
                    "extra_variant_tag",
                    path + [f"变体标签 {label}"],
                    f"发送端可能发出接收端未声明的标签 {label}",
                )
        return None


# ---------------------------------------------------------------------------
# 顶层审计
# ---------------------------------------------------------------------------


def validate_audit_id(audit_id: Any) -> str | None:
    if not isinstance(audit_id, str) or not AUDIT_ID_RE.match(audit_id or ""):
        return None
    return audit_id


def run_audit(payload: dict[str, Any]) -> dict[str, Any]:
    """编译双侧契约并给出审计结论（纯函数，不负责冻结存储）。"""
    sender = compile_side("sender", payload.get("sender"))
    receiver = compile_side("receiver", payload.get("receiver"))

    errors = sender.errors + receiver.errors
    # 稳定排序，保证相同输入字节级一致，便于冻结比对。
    errors.sort(key=lambda e: (e["where"], e["code"], e["message"]))

    if errors:
        return {
            "conclusion": "invalid",
            "entry": None,
            "violation": None,
            "errors": errors,
            "reused_pairs": [],
            "compared_pairs": 0,
        }

    report = check_compatibility(sender, receiver)
    report["errors"] = []
    return report
