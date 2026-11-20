"""Small, safe XML reader for KML and GDAL .aux.xml files.

Built directly on expat with entity declarations and external entities forbidden
(the protection defusedxml gives), so a hostile file cannot expand entities
("billion laughs") or make the parser read other files or URLs.

It builds a minimal tree with the parts of ElementTree the plugin uses:
``tag`` ("{namespace}name" like ElementTree), ``text``, ``attrib``, iterating the
children, ``iter()`` (the element and all its descendants) and ``find(tag)``.
"""
from __future__ import annotations

from pathlib import Path
from xml.parsers import expat

MAX_BYTES = 200 * 1024 * 1024        # KML / .aux.xml files are far smaller


class XmlError(ValueError):
    """The data is not valid XML, or uses features that are not allowed."""


class Element:
    __slots__ = ("tag", "attrib", "text", "children")

    def __init__(self, tag: str, attrib: dict):
        self.tag = tag
        self.attrib = attrib
        self.text: str | None = None
        self.children: list[Element] = []

    def __iter__(self):
        return iter(self.children)

    def __len__(self):
        return len(self.children)

    def iter(self):
        """This element and all its descendants, in document order."""
        stack = [self]
        while stack:
            elem = stack.pop()
            yield elem
            stack.extend(reversed(elem.children))

    def find(self, tag: str) -> Element | None:
        """First direct child with this tag, or None."""
        for child in self.children:
            if child.tag == tag:
                return child
        return None

    def get(self, key: str, default=None):
        return self.attrib.get(key, default)


def _forbidden(what: str):
    def handler(*_args):
        raise XmlError(f"XML {what} are not allowed.")
    return handler


def _qualified(name: str) -> str:
    # expat with namespace_separator="}" gives "uri}local"; ElementTree style is "{uri}local".
    return "{" + name if "}" in name else name


def parse_bytes(data: bytes) -> Element:
    """Root element of an XML document given as bytes."""
    if len(data) > MAX_BYTES:
        raise XmlError("The XML file is too large.")
    parser = expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.EntityDeclHandler = _forbidden("entity declarations")
    parser.UnparsedEntityDeclHandler = _forbidden("entity declarations")
    parser.ExternalEntityRefHandler = _forbidden("external entities")

    root: list[Element] = []
    stack: list[Element] = []
    texts: list[list[str]] = []

    def start(name, attrs):
        elem = Element(_qualified(name), {_qualified(k): v for k, v in attrs.items()})
        if stack:
            parent = stack[-1]
            if not parent.children and texts[-1]:
                parent.text = "".join(texts[-1])
            parent.children.append(elem)
        else:
            root.append(elem)
        stack.append(elem)
        texts.append([])

    def end(_name):
        elem = stack.pop()
        parts = texts.pop()
        if not elem.children and parts:
            elem.text = "".join(parts)

    def chars(data):
        if stack and not stack[-1].children:
            texts[-1].append(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise XmlError(str(exc)) from exc
    if not root:
        raise XmlError("The XML document is empty.")
    return root[0]


def parse_file(path: str | Path) -> Element:
    """Root element of an XML file."""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise XmlError(f"{path.name} is too large.")
    return parse_bytes(path.read_bytes())
