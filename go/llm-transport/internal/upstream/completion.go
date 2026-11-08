package upstream

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// parseCompletionBody 解析非 SSE 的一整段 chat completion 响应（契约 §4.9）。
//
// 个别网关会忽略 stream 参数直接回一整段 JSON，这是该情况的回退路径。
func parseCompletionBody(raw []byte, statusCode int) (*Result, error) {
	trimmed := bytes.TrimLeft(raw, " \t\r\n")
	if statusCode >= 200 && statusCode < 300 && (len(trimmed) == 0 || (trimmed[0] != '{' && trimmed[0] != '[')) {
		// 2xx 但不是 JSON：通常是反代/网关返回了 HTML 错误页。
		return nil, NewResponseError(
			fmt.Sprintf("提供商返回了非 JSON 响应（HTTP %d）：%s", statusCode, truncateRunes(string(raw), 200)),
			&statusCode,
		)
	}
	var generic any
	if err := json.Unmarshal(raw, &generic); err != nil {
		return nil, NewResponseError(
			fmt.Sprintf("提供商响应无法解析为 JSON：%v", err),
			&statusCode,
		)
	}
	if _, ok := generic.(map[string]any); !ok {
		return nil, NewResponseError("提供商响应的顶层结构不是对象。", &statusCode)
	}
	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		return nil, NewResponseError(
			fmt.Sprintf("提供商响应无法解析为 JSON：%v", err),
			&statusCode,
		)
	}

	var choices []json.RawMessage
	if !isNullJSON(top["choices"]) {
		if err := json.Unmarshal(top["choices"], &choices); err != nil {
			choices = nil
		}
	}
	if len(choices) == 0 {
		return nil, NewResponseError("提供商响应中没有 choices。", &statusCode)
	}
	var choice map[string]json.RawMessage
	if err := json.Unmarshal(choices[0], &choice); err != nil {
		return nil, NewResponseError("提供商响应中 message 字段无效。", &statusCode)
	}

	// message 缺失等价于空对象（Python 的 dict.get("message", {})），显式 null
	// 或非对象才算字段无效。
	message := map[string]json.RawMessage{}
	if rawMessage, ok := choice["message"]; ok {
		if isNullJSON(rawMessage) {
			return nil, NewResponseError("提供商响应中 message 字段无效。", &statusCode)
		}
		if err := json.Unmarshal(rawMessage, &message); err != nil {
			return nil, NewResponseError("提供商响应中 message 字段无效。", &statusCode)
		}
	}

	var usage json.RawMessage
	if !isNullJSON(top["usage"]) {
		var usageFields map[string]json.RawMessage
		if err := json.Unmarshal(top["usage"], &usageFields); err == nil && len(usageFields) > 0 {
			usage = append(json.RawMessage(nil), top["usage"]...)
		}
	}

	// reasoning_content 是模型的私有输出，内容为空时绝不能顶成对外正文。
	content := extractAssistantText(message, false)
	reasoning := normalizeText(decodeJSONValue(message["reasoning_content"]))
	if err := raiseIfContentPolicyBlock(content); err != nil {
		return nil, err
	}
	finishReason := "stop"
	if !isNullJSON(choice["finish_reason"]) {
		if finish := scalarField(choice["finish_reason"]); finish != "" {
			finishReason = finish
		}
	}
	return &Result{
		Content:          content,
		ReasoningContent: reasoning,
		ToolCalls:        parseToolCalls(message["tool_calls"]),
		FinishReason:     finishReason,
		Usage:            usage,
	}, nil
}

// assistantTextKeys 对应 Python extract_assistant_text 在 include_reasoning=false
// 时保留的字段顺序。
var assistantTextKeys = []string{"content", "output_text", "refusal"}

// reasoningTextKeys 是 include_reasoning=true 时的取字段顺序。
var reasoningTextKeys = []string{"content", "reasoning_content", "output_text", "refusal", "reasoning"}

// extractAssistantText 从异构 message 结构中取对外可见的正文。
func extractAssistantText(message map[string]json.RawMessage, includeReasoning bool) string {
	keys := assistantTextKeys
	if includeReasoning {
		keys = reasoningTextKeys
	}
	for _, key := range keys {
		if text := normalizeText(decodeJSONValue(message[key])); text != "" {
			return text
		}
	}
	return ""
}

// decodeJSONValue 把 RawMessage 解码成通用值；失败或为空返回 nil。
func decodeJSONValue(raw json.RawMessage) any {
	if isNullJSON(raw) {
		return nil
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil
	}
	return value
}

// normalizeText 复刻 Python 的 _normalize_text：字符串去空白，对象按固定键序取第一个
// 非空文本，数组用换行拼装。
func normalizeText(value any) string {
	switch typed := value.(type) {
	case string:
		return strings.TrimSpace(typed)
	case map[string]any:
		for _, key := range []string{"text", "content", "reasoning_content", "output_text", "value", "refusal"} {
			if text := normalizeText(typed[key]); text != "" {
				return text
			}
		}
		return ""
	case []any:
		parts := make([]string, 0, len(typed))
		for _, item := range typed {
			if text := normalizeText(item); text != "" {
				parts = append(parts, text)
			}
		}
		return strings.TrimSpace(strings.Join(parts, "\n"))
	}
	return ""
}
