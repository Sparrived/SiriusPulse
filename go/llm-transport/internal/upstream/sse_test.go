package upstream

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// sseLine 把一帧 JSON 包成单行 data: 帧。
func sseLine(t *testing.T, frame any) string {
	t.Helper()
	encoded, err := json.Marshal(frame)
	if err != nil {
		t.Fatalf("构造 SSE 帧失败: %v", err)
	}
	return "data: " + string(encoded)
}

func deltaFrame(delta map[string]any) map[string]any {
	return map[string]any{"choices": []any{map[string]any{"delta": delta}}}
}

// TestAggregateEventStreamContentReasoningAndUsage 覆盖多帧 content/reasoning/usage 聚合。
func TestAggregateEventStreamContentReasoningAndUsage(t *testing.T) {
	stream := strings.Join([]string{
		": keep-alive",
		"event: message",
		sseLine(t, deltaFrame(map[string]any{"reasoning_content": "先想"})),
		sseLine(t, map[string]any{"choices": []any{map[string]any{
			"delta":         map[string]any{"content": "你好"},
			"finish_reason": nil,
		}}}),
		sseLine(t, map[string]any{"choices": []any{map[string]any{
			"delta":         map[string]any{"content": "，月白"},
			"finish_reason": "stop",
		}}}),
		sseLine(t, map[string]any{"choices": []any{}, "usage": map[string]any{
			"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14,
		}}),
		// 空 usage 视为无（契约 §4.5），不应覆盖上一条。
		sseLine(t, map[string]any{"choices": []any{}, "usage": map[string]any{}}),
		"data: [DONE]",
	}, "\n")

	result, err := aggregateEventStream(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("聚合失败: %v", err)
	}
	if result.Content != "你好，月白" {
		t.Fatalf("content = %q", result.Content)
	}
	if result.ReasoningContent != "先想" {
		t.Fatalf("reasoning_content = %q", result.ReasoningContent)
	}
	if result.FinishReason != "stop" {
		t.Fatalf("finish_reason = %q", result.FinishReason)
	}
	wantUsage := `{"completion_tokens":3,"prompt_tokens":11,"total_tokens":14}`
	if string(result.Usage) != wantUsage {
		t.Fatalf("usage = %s, want %s", result.Usage, wantUsage)
	}
	if len(result.ToolCalls) != 0 {
		t.Fatalf("tool_calls = %v", result.ToolCalls)
	}
}

// TestAggregateEventStreamSkipsBadFrames 覆盖空行/注释/坏帧/非 data 行的跳过规则。
func TestAggregateEventStreamSkipsBadFrames(t *testing.T) {
	stream := strings.Join([]string{
		"",
		"   ",
		"event: ping",
		"id: 42",
		"retry: 1000",
		`data: {"choices":[{"delta":{"content":"半截`,
		`data: {not json}`,
		"data: ",
		sseLine(t, deltaFrame(map[string]any{"content": "活下来了"})),
		"data: [DONE]",
		sseLine(t, deltaFrame(map[string]any{"content": "DONE 之后不该出现"})),
	}, "\n")

	result, err := aggregateEventStream(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("聚合失败: %v", err)
	}
	if result.Content != "活下来了" {
		t.Fatalf("content = %q", result.Content)
	}
}

// TestAggregateEventStreamToolCallsMergedByIndex 覆盖分块拼接与按 index 升序输出。
func TestAggregateEventStreamToolCallsMergedByIndex(t *testing.T) {
	stream := strings.Join([]string{
		// index 1 先到、index 0 后到：输出必须按 index 升序。
		sseLine(t, deltaFrame(map[string]any{"tool_calls": []any{map[string]any{
			"index": 1, "id": "call_b", "type": "function",
			"function": map[string]any{"name": "second", "arguments": `{"b"`},
		}}})),
		sseLine(t, deltaFrame(map[string]any{"tool_calls": []any{map[string]any{
			"index": 0, "id": "call_a", "type": "function",
			"function": map[string]any{"name": "first", "arguments": `{"a":`},
		}}})),
		// 后续帧只带 arguments 增量。
		sseLine(t, deltaFrame(map[string]any{"tool_calls": []any{map[string]any{
			"index": 1, "function": map[string]any{"arguments": `:2}`},
		}}})),
		sseLine(t, map[string]any{"choices": []any{map[string]any{
			"delta": map[string]any{"tool_calls": []any{map[string]any{
				"index": 0, "function": map[string]any{"arguments": `1}`},
			}}},
			"finish_reason": "tool_calls",
		}}}),
		"data: [DONE]",
	}, "\n")

	result, err := aggregateEventStream(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("聚合失败: %v", err)
	}
	if len(result.ToolCalls) != 2 {
		t.Fatalf("tool_calls 数量 = %d", len(result.ToolCalls))
	}
	first, second := result.ToolCalls[0], result.ToolCalls[1]
	if first.ID != "call_a" || first.FunctionName != "first" || first.FunctionArguments != `{"a":1}` {
		t.Fatalf("第一个工具调用 = %+v", first)
	}
	if second.ID != "call_b" || second.FunctionName != "second" || second.FunctionArguments != `{"b":2}` {
		t.Fatalf("第二个工具调用 = %+v", second)
	}
	if first.Type != "function" || second.Type != "function" {
		t.Fatalf("工具类型 = %q/%q", first.Type, second.Type)
	}
	if result.FinishReason != "tool_calls" {
		t.Fatalf("finish_reason = %q", result.FinishReason)
	}
}

// TestAggregateEventStreamContentPolicyMarkers 覆盖三个内容策略标记（大小写/空白不敏感）。
func TestAggregateEventStreamContentPolicyMarkers(t *testing.T) {
	markers := []string{
		"The prompt could not be submitted. Please try again.",
		"  PROMPT CONTAINS SENSITIVE WORDS  ",
		"Blocked by the Generative AI Prohibited Use Policy.",
	}
	for _, marker := range markers {
		stream := strings.Join([]string{
			sseLine(t, deltaFrame(map[string]any{"content": marker})),
			sseLine(t, map[string]any{"usage": map[string]any{"total_tokens": 9}}),
			"data: [DONE]",
		}, "\n")
		_, err := aggregateEventStream(strings.NewReader(stream))
		var upstreamErr *Error
		if !errors.As(err, &upstreamErr) {
			t.Fatalf("标记 %q 未触发失败: %v", marker, err)
		}
		if upstreamErr.Code != contract.CodeProviderContentFiltered {
			t.Fatalf("标记 %q 错误码 = %s", marker, upstreamErr.Code)
		}
		if upstreamErr.HTTPStatus != contract.StatusProviderFailure || upstreamErr.Retryable {
			t.Fatalf("标记 %q 状态/可重试 = %d/%v", marker, upstreamErr.HTTPStatus, upstreamErr.Retryable)
		}
		if upstreamErr.ContentPreview == "" {
			t.Fatalf("标记 %q 缺少 content_preview", marker)
		}
	}

	// 正常回复里出现相似词不应误判。
	stream := strings.Join([]string{
		sseLine(t, deltaFrame(map[string]any{"content": "这段 prompt 里没有敏感词。"})),
		"data: [DONE]",
	}, "\n")
	if _, err := aggregateEventStream(strings.NewReader(stream)); err != nil {
		t.Fatalf("正常回复被误判: %v", err)
	}
}

// TestAggregateEventStreamErrorEvent 覆盖流内 error 事件（契约 §4.8）。
func TestAggregateEventStreamErrorEvent(t *testing.T) {
	cases := []struct {
		name    string
		frame   map[string]any
		message string
	}{
		{"取 message", map[string]any{"error": map[string]any{"message": "上游崩了", "code": "x"}}, "提供商流式响应错误：上游崩了"},
		{"退 code", map[string]any{"error": map[string]any{"code": "rate_limited"}}, "提供商流式响应错误：rate_limited"},
		{"退字符串化", map[string]any{"error": "boom"}, "提供商流式响应错误：boom"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			stream := strings.Join([]string{sseLine(t, testCase.frame), "data: [DONE]"}, "\n")
			_, err := aggregateEventStream(strings.NewReader(stream))
			var upstreamErr *Error
			if !errors.As(err, &upstreamErr) {
				t.Fatalf("未触发失败: %v", err)
			}
			if upstreamErr.Code != contract.CodeProviderResponseError {
				t.Fatalf("错误码 = %s", upstreamErr.Code)
			}
			if upstreamErr.Message != testCase.message {
				t.Fatalf("message = %q", upstreamErr.Message)
			}
			if upstreamErr.UpstreamStatus != nil {
				t.Fatalf("upstream_status 应为 nil，实际 %v", *upstreamErr.UpstreamStatus)
			}
			if !upstreamErr.Retryable {
				t.Fatal("无状态码的流内错误应可重试")
			}
		})
	}
}

// TestAggregateEventStreamEmptyAndTruncated 覆盖空流与无 [DONE] 的截断流。
func TestAggregateEventStreamEmptyAndTruncated(t *testing.T) {
	result, err := aggregateEventStream(strings.NewReader(""))
	if err != nil {
		t.Fatalf("空流不应报错: %v", err)
	}
	if result.Content != "" || len(result.ToolCalls) != 0 || result.FinishReason != "stop" || result.Usage != nil {
		t.Fatalf("空流结果 = %+v", result)
	}

	truncated := sseLine(t, deltaFrame(map[string]any{"content": "没有结束标记"}))
	result, err = aggregateEventStream(strings.NewReader(truncated))
	if err != nil {
		t.Fatalf("截断流不应报错: %v", err)
	}
	if result.Content != "没有结束标记" {
		t.Fatalf("截断流 content = %q", result.Content)
	}
}
