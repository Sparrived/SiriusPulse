package upstream

import (
	"encoding/json"
	"strings"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// Result 是一次成功生成在传输层聚合出的原始结果。
//
// 它对应 Python 的 GenerationResult 加上传输统计；server 层按契约 §3 把它
// 组装成 HTTP 响应。Usage 保存上游原始 usage 对象，不得改写。
type Result struct {
	Content          string
	ReasoningContent string
	ToolCalls        []contract.ToolCall
	FinishReason     string
	Usage            json.RawMessage
	// ContentType 是上游响应的 Content-Type 原文。
	ContentType string
	// Path 是 "sse" 或 "json"（契约 §3 transport.path）。
	Path string
	// UpstreamMs 是本次上游调用的耗时（毫秒，含流式聚合）。
	UpstreamMs float64
}

// truncateRunes 按字符（而不是字节）截断，与 Python 的 raw[:n] 语义一致。
func truncateRunes(text string, limit int) string {
	if limit <= 0 {
		return ""
	}
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	return string(runes[:limit])
}

// containsSubstring 是大小写敏感的包含判断，仅用于固定英文错误片段。
func containsSubstring(text string, sub string) bool {
	return strings.Contains(text, sub)
}

// stringField 读取一个 JSON 字符串字段；非字符串或缺失返回空串。
func stringField(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var text string
	if err := json.Unmarshal(raw, &text); err != nil {
		return ""
	}
	return text
}

// scalarField 读取一个 JSON 标量字段并转成文本（字符串原样，其余取 JSON 文本）。
func scalarField(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return text
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return ""
	}
	switch value.(type) {
	case nil:
		return ""
	case map[string]any, []any:
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// isNullJSON 判断原始 JSON 是否为 null 或空。
func isNullJSON(raw json.RawMessage) bool {
	trimmed := strings.TrimSpace(string(raw))
	return trimmed == "" || trimmed == "null"
}
