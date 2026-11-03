# AGENTS

This repository is maintained by both humans and AI coding agents.

All coding agents Should follow below rules and principles.

## 1. Rules

must comply with:

- do not modify exist harnesses yaml, always derive new version if need.
- Prefer self-documenting code. Write comments only when the “why” cannot be clearly expressed by the code itself. Keep them short and accurate.

## 2. Coding Principles

- Prioritize modular design with high cohesion and low coupling, ensure scalability and extensibility for future growth, and maintain high maintainability with strong observability
- Avoid over-engineering. Choose the simplest design that works, and only introduce abstraction when there is a clear, recurring need.

## 3. Tool Feedback Principle

Tool error messages state only verified facts, never guess intent.

- Say what happened for example: `cannot read file 'x': ...`, `old_text is empty`,
  `edit 2 matches 0 locations`, not what the model wanted
  (maybe it meant to create a file, maybe it just typo'd the path —
  we cannot know).
- Do not prescribe the next tool (`use write_file ...`, `verify with
  find_files`, `use edit_file instead`). Routing decisions belong to
  the model and the harness prompt, not to error text.
