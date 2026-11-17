"""Bounded, deterministic when seeded, literal-only creative templates."""
from dataclasses import dataclass
from random import Random
import re


VARIABLES = frozenset({"channel_link", "channel_name", "price", "subscription_period",
                       "campaign_name", "description", "topic", "platform"})
LEGACY = {"link": "channel_link", "channel": "channel_name", "price": "price",
          "description": "description", "topic": "topic", "platform": "platform"}
COUNT_LIMIT = 1_000_001
MAX_OUTPUT = 16384


class SpintaxError(ValueError):
    def __init__(self, source, offset, message):
        self.offset = offset
        self.line = source.count("\n", 0, offset) + 1
        self.column = offset - source.rfind("\n", 0, offset)
        super().__init__(f"{self.line}:{self.column}: {message}")


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Variable:
    name: str
    offset: int


@dataclass(frozen=True)
class Sequence:
    items: tuple


@dataclass(frozen=True)
class Choice:
    branches: tuple


class Parser:
    def __init__(self, source):
        self.source = source
        self.offset = 0
        self.nodes = 0
        try:
            size = len(source.encode("utf-8"))
        except UnicodeEncodeError:
            self.fail("Некорректный Unicode")
        if size > 16384:
            self.fail("Шаблон больше 16 KiB")

    def fail(self, message):
        raise SpintaxError(self.source, self.offset, message)

    def node(self, item):
        self.nodes += 1
        if self.nodes > 4096:
            self.fail("Слишком много элементов")
        return item

    def sequence(self, depth):
        items, text = [], []
        while self.offset < len(self.source):
            char = self.source[self.offset]
            if char in "|}":
                if depth:
                    break
                self.fail("Непарная скобка или разделитель")
            if char == "\\":
                self.offset += 1
                escaped = self.source[self.offset:self.offset + 1]
                if escaped not in ("{", "}", "|", "\\"):
                    self.fail("Ожидается экранируемый символ")
                text.append(escaped)
                self.offset += 1
            elif char == "{":
                if text:
                    items.append(self.node(Text("".join(text))))
                    text.clear()
                if self.source.startswith("{{", self.offset):
                    start = self.offset
                    close = self.source.find("}}", start + 2)
                    name = self.source[start + 2:close] if close != -1 else ""
                    if name not in VARIABLES:
                        self.fail("Неизвестная или незакрытая переменная")
                    self.offset = close + 2
                    items.append(self.node(Variable(name, start)))
                else:
                    items.append(self.choice(depth + 1))
            else:
                if ord(char) < 32 and char not in "\n\r\t":
                    self.fail("Недопустимый управляющий символ")
                text.append(char)
                self.offset += 1
        if text:
            items.append(self.node(Text("".join(text))))
        return self.node(Sequence(tuple(items)))

    def choice(self, depth):
        if depth > 16:
            self.fail("Слишком глубокая вложенность")
        branches = []
        while True:
            self.offset += 1
            branch = self.sequence(depth)
            if not branch.items or all(isinstance(x, Text) and not x.value.strip() for x in branch.items):
                self.fail("Пустой вариант")
            branches.append(branch)
            if len(branches) > 32:
                self.fail("Слишком много вариантов")
            delimiter = self.source[self.offset:self.offset + 1]
            if delimiter == "}":
                self.offset += 1
                break
            if delimiter != "|":
                self.fail("Ожидается | или }")
        if len(branches) < 2:
            self.fail("Ожидаются минимум два варианта")
        return self.node(Choice(tuple(branches)))


def parse(source):
    return Parser(source).sequence(0)


def combination_count(node):
    if isinstance(node, Choice):
        count = 0
        for branch in node.branches:
            count = min(COUNT_LIMIT, count + combination_count(branch))
        return count
    if isinstance(node, Sequence):
        count = 1
        for part in node.items:
            count = min(COUNT_LIMIT, count * combination_count(part))
        return count
    return 1


def generate(source, values, limit=1, seed=None):
    if not 1 <= limit <= 10:
        raise ValueError("Expected 1 to 10 variants")
    tree = parse(source)
    rng = Random(seed)

    def validate(node):
        if isinstance(node, Variable):
            value = values.get(node.name)
            if (not isinstance(value, str) or not value.strip()
                    or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in value)):
                raise SpintaxError(source, node.offset, "Отсутствует или недопустима переменная")
        elif isinstance(node, (Sequence, Choice)):
            for part in node.items if isinstance(node, Sequence) else node.branches:
                validate(part)

    def parts(node):
        if isinstance(node, Text):
            yield node.value
        elif isinstance(node, Variable):
            yield values[node.name]
        elif isinstance(node, Choice):
            yield from parts(rng.choice(node.branches))
        else:
            for item in node.items:
                yield from parts(item)

    validate(tree)
    variants = {}
    maximum = min(limit, combination_count(tree))
    for _ in range(100):
        pieces, length = [], 0
        for part in parts(tree):
            length += len(part)
            if length > MAX_OUTPUT:
                raise SpintaxError(source, 0, "Результат слишком длинный")
            pieces.append(part)
        variants["".join(pieces)] = None
        if len(variants) == maximum:
            break
    return tuple(variants)


def convert_legacy(source):
    """Translate supported v2 placeholders and escape every other literal."""
    pattern = r"\{(" + "|".join(LEGACY) + r")\}|([{}|\\])"

    def replacement(match):
        if match[1] is not None:
            return "{{" + LEGACY[match[1]] + "}}"
        return "\\" + match[2]

    return re.sub(pattern, replacement, source)

