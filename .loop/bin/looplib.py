"""Shared text rules for .loop/bin scripts. Stdlib only; YAML parsing lives in load_contract."""
import hashlib
import re

SENTINEL = "<!-- LOOP CONTRACT v1 -->"
BEHAVIORAL, MECHANICAL = "behavioral", "mechanical"


def _lines(text):
    return text.replace("\r\n", "\n").split("\n")


def split_body(body):
    """(human text above the sentinel, text from the sentinel on | None)."""
    lines = _lines(body)
    for i, line in enumerate(lines):
        if line.strip() == SENTINEL:
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
    return "\n".join(lines).rstrip(), None


def contract_hash(contract_part):
    return hashlib.sha256(contract_part.replace("\r\n", "\n").strip().encode()).hexdigest()


def extract_contract(text):
    """Canonical contract (sentinel + yaml fence) from planner output or an issue body, or None."""
    lines = _lines(text)
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == SENTINEL)
    except StopIteration:
        return None
    body, inside = [], False
    for line in lines[start + 1:]:
        s = line.strip()
        if not inside:
            if s in ("```yaml", "```yml"):
                inside = True
            elif s:
                return None  # anything but blank lines between sentinel and fence
        elif s == "```":
            return SENTINEL + "\n```yaml\n" + "\n".join(body).rstrip() + "\n```"
        else:
            body.append(line)
    return None


def contract_yaml(contract_part):
    lines = _lines(contract_part)
    return "\n".join(lines[2:-1])  # canonical form: sentinel, ```yaml, ..., ```


def _section(human, heading):
    out, inside = [], False
    for line in _lines(human):
        if re.match(r"^#{1,2}\s", line):
            inside = re.match(r"^##\s+" + heading + r"\s*$", line, re.I) is not None
            continue
        if inside:
            out.append(line)
    return out


def acceptance_criteria(human):
    """Top-level list items under `## Acceptance criteria`, in order."""
    item = re.compile(r"^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)")
    return [m.group(1) for m in map(item.match, _section(human, "Acceptance criteria")) if m]


def blocked_by(human):
    return [int(n) for line in _section(human, "Blocked by") for n in re.findall(r"#(\d+)", line)]


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "ticket"


def load_contract(contract_part):
    import yaml  # only available under `uv run --with pyyaml`

    return yaml.safe_load(contract_yaml(contract_part))
