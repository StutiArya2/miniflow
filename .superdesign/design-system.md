# MiniFlow Studio design system

MiniFlow is a local, single-user low-code workflow application. Keep the existing blue MiniFlow identity and status colors. The visual editor should feel like a practical desktop workbench: compact, legible, and calm, with the graph as the focus.

## Existing tokens
- Light: background #f4f6f9, panels #ffffff, text #1b2330, accent #2f6fdb, border #dfe4eb.
- Dark: background #0f141b, panels #161c25, text #e3e8ef, accent #5b93f0, border #2a3341.
- Task states use distinct pending, ready, running, retrying, success, failed, and skipped colors from `miniflow/ui/static/style.css`.
- System sans font for UI; system monospace for IDs, expressions, and data previews.

## Editor structure
Retain the existing top bar and dashboard styling. Add an Editor tab or workspace that has: a narrow searchable block palette on the left, a large central canvas with connected task cards, and a settings panel on the right. A compact toolbar holds flow name, Save, Validate, Test, and Run. Show validation problems close to their nodes and in a list. Let users draw dependencies, configure settings, inspect placeholder expressions, set flow parameters and environments, and switch to recent run results. Use the saved JSON flow schema as the source of truth. At narrow widths, collapse palette and settings into drawers without horizontal page scrolling.

## Behavior
A new user should open a template, edit a parameter, add and connect a block, configure settings, validate, save, and run without editing JSON. Preserve access to the existing runs graph and timeline. Labels and errors use plain language.
