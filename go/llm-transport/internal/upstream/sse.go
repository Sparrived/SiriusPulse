package upstream

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

// contentPolicyMarkers 是上游把「内容策略拦截」的说明文字当成正常正文返回时的识别标记。
//
// 目前在 Google/Gemini 经 CPA 的 antigravity 通道观测到：prompt 命中 Prohibited Use
// 策略后仍回 HTTP 200、没有 error 事件、usage 全 0，正文就是这句拒绝文案。
// 只取拒绝文案里稳定出现的片段，避免上游改措辞后立刻失效。
var contentPolicyMarkers = []string{
	"the prompt could not be submitted",
	"prompt contains sensitive words",
	"generative ai prohibited use policy",
}

// isContentPolicyBlock 判断一段模型输出是否是上游的内容策略拦截文案（契约 §4.7）。
func isContentPolicyBlock(text string) bool {
	normalized := strings.ToLower(strings.TrimSpace(text))
	if normalized == "" {
		return false
	}
	for _, marker := range contentPolicyMarkers {
		if strings.Contains(normalized, marker) {
			return true
		}
	}
	return false
}

// raiseIfContentPolicyBlock 把「拒绝文案当正文」的上游响应转成明确的失败。
//
// usage 一并丢弃：这次调用没有产生任何可用输出，留着会让调用方把 0 用量当成真实统计。
func raiseIfContentPolicyBlock(content string) error {
	if !isContentPolicyBlock(content) {
		return nil
	}
	return NewContentFilteredError("提供商把内容策略拦截的说明文字当作正常回复返回。", content)
}

// aggregator 累积一条 SSE 流的增量（契约 §4.4）。
type aggregator struct {
	content      strings.Builder
	reasoning    strings.Builder
	toolCalls    toolCallAccumulator
	finishReason string
	usage        json.RawMessage
}

func newAggregator() *aggregator {
	return &aggregator{
		toolCalls:    toolCallAccumulator{},
		finishReason: "stop",
	}
}

// aggregateEventStream 把一条 OpenAI 兼容 SSE 流聚合成完整结果。
//
// 只认单行 data: 帧（OpenAI 兼容流不会把一个 JSON 拆到多行），多行 data 会被
// 当成坏帧跳过；跳过坏帧而不是整段失败，是为了不被上游心跳或半截帧毁掉整段回复。
func aggregateEventStream(reader io.Reader) (*Result, error) {
	acc := newAggregator()
	buffered := bufio.NewReaderSize(reader, 64*1024)
	for {
		line, readErr := buffered.ReadString('\n')
		if len(line) > 0 {
			done, err := acc.consumeLine(line)
			if err != nil {
				return nil, err
			}
			if done {
				break
			}
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				break
			}
			return nil, readErr
		}
	}
	return acc.result()
}

// consumeLine 处理一行 SSE 文本；done 表示看到了 data: [DONE]。
func (a *aggregator) consumeLine(line string) (bool, error) {
	line = strings.TrimSpace(line)
	if line == "" || strings.HasPrefix(line, ":") || strings.HasPrefix(line, "event:") {
		return false, nil
	}
	if !strings.HasPrefix(line, "data:") {
		return false, nil
	}
	data := strings.TrimSpace(line[len("data:"):])
	if data == "" {
		return false, nil
	}
	if data == "[DONE]" {
		return true, nil
	}
	var frame map[string]json.RawMessage
	if err := json.Unmarshal([]byte(data), &frame); err != nil {
		// 心跳或半截帧：跳过这一帧，不整段失败（契约 §4.3）。
		return false, nil
	}
	a.applyUsage(frame["usage"])
	if err := a.applyError(frame["error"]); err != nil {
		return false, err
	}
	a.applyChoices(frame["choices"])
	return false, nil
}

// applyUsage 取最后一个非空 usage（契约 §4.5）；空对象 {} 视为无。
func (a *aggregator) applyUsage(raw json.RawMessage) {
	if isNullJSON(raw) {
		return
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return
	}
	if len(fields) == 0 {
		return
	}
	a.usage = append(json.RawMessage(nil), raw...)
}

// applyError 处理流内 error 事件（契约 §4.8）：AMKR 在上游流中断时补发它而不是正常结束。
func (a *aggregator) applyError(raw json.RawMessage) error {
	if isNullJSON(raw) {
		return nil
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil
	}
	if !truthyJSON(value) {
		return nil
	}
	return NewResponseError("提供商流式响应错误："+errorMessage(value), nil)
}

// applyChoices 累积 choices[].delta 的增量内容。
func (a *aggregator) applyChoices(raw json.RawMessage) {
	if isNullJSON(raw) {
		return
	}
	var choices []json.RawMessage
	if err := json.Unmarshal(raw, &choices); err != nil {
		return
	}
	for _, item := range choices {
		var choice map[string]json.RawMessage
		if err := json.Unmarshal(item, &choice); err != nil {
			continue
		}
		if deltaRaw, ok := choice["delta"]; ok && !isNullJSON(deltaRaw) {
			var delta map[string]json.RawMessage
			if err := json.Unmarshal(deltaRaw, &delta); err == nil {
				if text := stringField(delta["reasoning_content"]); text != "" {
					a.reasoning.WriteString(text)
				}
				if text := stringField(delta["content"]); text != "" {
					a.content.WriteString(text)
				}
				a.toolCalls.merge(delta["tool_calls"])
			}
		}
		if finish := scalarField(choice["finish_reason"]); finish != "" {
			a.finishReason = finish
		}
	}
}

// result 产出聚合结果，并在末尾做内容策略检查。
func (a *aggregator) result() (*Result, error) {
	content := a.content.String()
	if err := raiseIfContentPolicyBlock(content); err != nil {
		return nil, err
	}
	return &Result{
		Content:          content,
		ReasoningContent: a.reasoning.String(),
		ToolCalls:        a.toolCalls.finalize(),
		FinishReason:     a.finishReason,
		Usage:            a.usage,
	}, nil
}

// errorMessage 取 error.message，退 error.code，再退整个 error 的字符串化（契约 §4.8）。
func errorMessage(value any) string {
	if fields, ok := value.(map[string]any); ok {
		if message, ok := fields["message"]; ok && truthyJSON(message) {
			return jsonScalarText(message)
		}
		if code, ok := fields["code"]; ok && truthyJSON(code) {
			return jsonScalarText(code)
		}
	}
	return jsonScalarText(value)
}

// jsonScalarText 把任意 JSON 值转成稳定的可读文本。
//
// 字符串原样返回，其余按紧凑 JSON 输出；Python 用的是对象的 repr（{'a': 1}），
// 这里改成等价的 JSON 文本，只是文案差异，不影响错误语义。
func jsonScalarText(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return fmt.Sprint(value)
	}
	return string(encoded)
}

// truthyJSON 复刻 Python 的真值判断，用于决定 error 事件是否真的存在。
func truthyJSON(value any) bool {
	switch typed := value.(type) {
	case nil:
		return false
	case bool:
		return typed
	case string:
		return typed != ""
	case float64:
		return typed != 0
	case map[string]any:
		return len(typed) > 0
	case []any:
		return len(typed) > 0
	}
	return true
}
