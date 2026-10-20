"""Server-only OpenAI Responses adapter. Credentials never enter reports or UI."""
import asyncio
import hashlib
import json

from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError

import review


class OpenAIReviewer:
    def __init__(self, api_key="", model="gpt-5.4", client_factory=None):
        self._api_key = api_key
        self.model = model
        self.client_factory = client_factory or AsyncOpenAI

    def status(self):
        return {"ready": bool(self._api_key), "provider": "openai", "model": self.model,
                "message": f"OpenAI API 설정됨 · {self.model} · 서버 사용량 적용" if self._api_key
                else "OpenAI API 키가 필요합니다. 서버의 .env에 설정하세요. 빠른 형식 검사는 사용할 수 있습니다."}

    def __call__(self, snapshot, item, cancel, progress=None):
        return review.ai_review(snapshot, item, cancel, progress=progress, pass_reviewer=self.pass_review)

    async def request(self, payload, cancel, timeout):
        async with self.client_factory(api_key=self._api_key, timeout=timeout, max_retries=0) as client:
            task = asyncio.create_task(client.responses.create(**payload))
            try:
                while not task.done():
                    if cancel.is_set():
                        raise review.Cancelled()
                    await asyncio.wait({task}, timeout=0.2)
                if cancel.is_set():
                    raise review.Cancelled()
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    def pass_review(self, snapshot, item, cancel, timeout=360, direction="forward", prior=None):
        if cancel.is_set():
            raise review.Cancelled()
        if not self._api_key:
            raise ValueError("서버의 OpenAI API 키를 설정하세요.")
        prompt = review.ai_prompt(snapshot, item, direction, prior)
        if len(prompt) > 140_000:
            raise ValueError("세트 데이터가 너무 큽니다. 범위를 나눠 검토하세요.")
        schema_name = "ai-recheck-schema.json" if direction == "reverse" else "ai-schema.json"
        payload = {"model": self.model, "input": prompt, "store": False,
                   "reasoning": {"effort": "medium"}, "max_output_tokens": 12000,
                   "text": {"format": {"type": "json_schema", "name": "readai_review",
                                      "schema": json.loads((review.ROOT/schema_name).read_text()), "strict": True}}}
        try:
            response = asyncio.run(self.request(payload, cancel, timeout))
        except review.Cancelled:
            raise
        except APITimeoutError:
            raise ValueError("OpenAI 응답 시간이 초과되었습니다. 해당 단계는 미완료입니다.") from None
        except APIStatusError as exc:
            message = {401: "서버의 OpenAI API 키를 확인하세요.",
                       403: "OpenAI가 요청을 거부했습니다(403). 프로젝트·키·모델 접근 권한을 확인하세요.",
                       429: "OpenAI 사용 한도 또는 요청 한도에 도달했습니다.",
                       400: "모델과 구조화 출력 설정을 확인하세요."}.get(exc.status_code, "OpenAI가 요청을 처리하지 못했습니다.")
            error_type = review.AIUnavailable if exc.status_code in (401, 403, 429) else ValueError
            raise error_type(message) from None
        except APIConnectionError:
            raise ValueError("OpenAI 연결에 실패했습니다. 서버 네트워크를 확인하세요.") from None
        if response.status != "completed" or not response.output_text:
            raise ValueError("OpenAI의 출력이 완성되지 않았습니다. 해당 단계는 미완료입니다.")
        try:
            result = json.loads(response.output_text)
        except (ValueError, TypeError):
            raise ValueError("OpenAI의 구조화 응답을 읽을 수 없습니다.") from None
        findings = review.validate_ai(result, snapshot, item)
        rechecks = review.validate_rechecks(result, prior or [], findings) if direction == "reverse" else []
        sections = dict(result["section_results"])
        for tab in review.TABS:
            if (any(i.get("ai_severity") for i in findings if i["sheet"] == tab)
                    and not any(i["severity"] == "error" for i in findings if i["sheet"] == tab)
                    and sections[tab] == "수정 필요"):
                sections[tab] = "확인 필요"
        summary = " ".join(result["summary"].split())[:180]
        if any(i.get("ai_severity") for i in findings):
            summary = f"AI가 {len(findings)}개 검토 후보를 제시했으며, 일부 판정은 추가 확인이 필요합니다."
        usage = response.usage
        return {"summary": summary, "issues": findings, "difficulty": result["difficulty"],
                "section_results": sections, "rechecks": rechecks, "direction": direction,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "provider": "openai", "model": self.model, "response_id": response.id,
                "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                          "total_tokens": usage.total_tokens} if usage else None}
