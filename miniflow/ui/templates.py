"""Self-contained starter workflows for the local visual editor."""

from __future__ import annotations

from typing import Any

SAMPLE_SALES = """order_id,region,amount
1,North,250
2,South,75
3,North,300
4,West,180
5,South,120
"""

TEMPLATES = (
    {"name": "hello_world", "description": "A two-step greeting you can edit and run immediately."},
    {
        "name": "sales_report",
        "description": "Filter sample sales, rank regions and save a CSV report.",
    },
)


def build_template(template: str, name: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Return the flow document and relative sample files to create beside it."""
    if template == "hello_world":
        return {
            "description": "A short greeting with a parameter",
            "params": {"person": {"type": "str", "default": "World", "help": "Who to greet"}},
            "steps": {
                "greet": {"block": "log", "settings": {"message": "Hello {{ params.person }}"}},
                "finish": {
                    "block": "log",
                    "after": ["greet"],
                    "settings": {"message": "Greeting complete"},
                },
            },
        }, {}
    if template == "sales_report":
        source = f"data/{name}_sales.csv"
        return {
            "description": "Revenue by region from sample sales above a minimum amount",
            "params": {
                "minimum": {
                    "type": "float",
                    "default": 100,
                    "help": "Only include orders above this amount",
                }
            },
            "steps": {
                "sales": {"block": "read_csv", "settings": {"path": source}},
                "selected": {
                    "block": "filter",
                    "settings": {"rows": "{{ sales }}", "where": "amount >= {{ params.minimum }}"},
                },
                "by_region": {
                    "block": "aggregate",
                    "settings": {
                        "rows": "{{ selected }}",
                        "group_by": "region",
                        "metrics": {"orders": "count", "revenue": "sum(amount)"},
                    },
                },
                "ranked": {
                    "block": "sort",
                    "settings": {"rows": "{{ by_region }}", "by": "revenue", "descending": True},
                },
                "save": {
                    "block": "write_csv",
                    "settings": {"rows": "{{ ranked }}", "path": f"out/{name}_report.csv"},
                },
                "summary": {
                    "block": "log",
                    "after": ["save"],
                    "settings": {
                        "message": (
                            "Saved {{ selected.count }} orders; "
                            "top region {{ ranked.first.region }}"
                        )
                    },
                },
            },
        }, {source: SAMPLE_SALES}
    raise KeyError(template)
