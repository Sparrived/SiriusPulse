from __future__ import annotations

import argparse
import json

from sirius_pulse import (
    PersonaSpec,
    RolePlayAnswer,
    generate_humanized_roleplay_questions,
    list_roleplay_question_templates,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出指定人格问卷模板的高层问题骨架")
    parser.add_argument("--template", default="default", help="问卷模板名，如 companion / romance / group_chat")
    parser.add_argument("--agent-name", default="北辰", help="示例人格名称")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    template = args.template.strip()

    questions = generate_humanized_roleplay_questions(template=template)
    spec = PersonaSpec(
        agent_name=args.agent_name,
        answers=[
            RolePlayAnswer(
                question=item.question,
                answer="",
                perspective=item.perspective,
                details=item.details,
            )
            for item in questions
        ],
    )

    payload = {
        "available_templates": list_roleplay_question_templates(),
        "selected_template": template,
        "persona_spec_skeleton": {
            "agent_name": spec.agent_name,
            "trait_keywords": spec.trait_keywords,
            "background": spec.background,
            "dependency_files": spec.dependency_files,
            "output_language": spec.output_language,
            "answers": [
                {
                    "question": item.question,
                    "perspective": item.perspective,
                    "details": item.details,
                    "answer": item.answer,
                }
                for item in spec.answers
            ],
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()