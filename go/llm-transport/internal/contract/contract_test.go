package contract

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// TestGenerateResponseWireShape 锁定契约 §3 的字段名与空值形态。
func TestGenerateResponseWireShape(t *testing.T) {
	encoded, err := json.Marshal(GenerateResponse{
		Content:          "hi",
		ReasoningContent: "",
		ToolCalls:        []ToolCall{},
		FinishReason:     "stop",
		Usage:            nil,
		Transport: Transport{
			ContentType:           "text/event-stream",
			LocalImageConversions: 1,
			UpstreamMs:            2190.5,
			Path:                  PathSSE,
		},
	})
	if err != nil {
		t.Fatalf("编码失败: %v", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatalf("解码失败: %v", err)
	}
	for _, key := range []string{"content", "reasoning_content", "tool_calls", "finish_reason", "usage", "transport"} {
		if _, ok := fields[key]; !ok {
			t.Fatalf("缺少字段 %s: %s", key, encoded)
		}
	}
	// 空 tool_calls 必须是 []，usage 必须原样输出为 null。
	if string(fields["tool_calls"]) != "[]" {
		t.Fatalf("tool_calls = %s", fields["tool_calls"])
	}
	if string(fields["usage"]) != "null" {
		t.Fatalf("usage = %s", fields["usage"])
	}
	var transport map[string]json.RawMessage
	if err := json.Unmarshal(fields["transport"], &transport); err != nil {
		t.Fatalf("transport 结构异常: %v", err)
	}
	for _, key := range []string{"content_type", "local_image_conversions", "upstream_ms", "path"} {
		if _, ok := transport[key]; !ok {
			t.Fatalf("transport 缺少字段 %s", key)
		}
	}
}

// TestGenerateResponseKeepsUsageVerbatim 覆盖 usage 原样透传（不得改写）。
func TestGenerateResponseKeepsUsageVerbatim(t *testing.T) {
	encoded, err := json.Marshal(GenerateResponse{
		Usage: json.RawMessage(`{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"extra":{"k":"v"}}`),
	})
	if err != nil {
		t.Fatalf("编码失败: %v", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatalf("解码失败: %v", err)
	}
	want := `{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"extra":{"k":"v"}}`
	if string(fields["usage"]) != want {
		t.Fatalf("usage = %s", fields["usage"])
	}
}

// TestErrorResponseWireShape 锁定契约 §6 的错误体字段。
func TestErrorResponseWireShape(t *testing.T) {
	upstreamStatus := 401
	encoded, err := json.Marshal(ErrorResponse{Error: ErrorBody{
		ErrorCode:      CodeProviderAuthError,
		Message:        "提供商 HTTP 错误 401：unauthorized",
		HTTPStatus:     StatusProviderFailure,
		UpstreamStatus: &upstreamStatus,
		IsRetryable:    false,
	}})
	if err != nil {
		t.Fatalf("编码失败: %v", err)
	}
	var body map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &body); err != nil {
		t.Fatalf("解码失败: %v", err)
	}
	var errorFields map[string]json.RawMessage
	if err := json.Unmarshal(body["error"], &errorFields); err != nil {
		t.Fatalf("error 结构异常: %v", err)
	}
	if string(errorFields["error_code"]) != `"PROVIDER_AUTH_ERROR"` {
		t.Fatalf("error_code = %s", errorFields["error_code"])
	}
	if string(errorFields["http_status"]) != "502" {
		t.Fatalf("http_status = %s", errorFields["http_status"])
	}
	if string(errorFields["upstream_status"]) != "401" {
		t.Fatalf("upstream_status = %s", errorFields["upstream_status"])
	}
	if string(errorFields["is_retryable"]) != "false" {
		t.Fatalf("is_retryable = %s", errorFields["is_retryable"])
	}
	if _, exists := errorFields["content_preview"]; exists {
		t.Fatalf("非内容策略错误不应带 content_preview: %s", encoded)
	}

	// upstream_status 缺省时必须是 null，content_preview 才出现。
	encoded, err = json.Marshal(ErrorResponse{Error: ErrorBody{
		ErrorCode:      CodeUpstreamTimeout,
		HTTPStatus:     StatusUpstreamTimeout,
		IsRetryable:    true,
		ContentPreview: "拒绝文案",
	}})
	if err != nil {
		t.Fatalf("编码失败: %v", err)
	}
	if !strings.Contains(string(encoded), `"upstream_status":null`) {
		t.Fatalf("upstream_status 应为 null: %s", encoded)
	}
	if !strings.Contains(string(encoded), `"content_preview":"拒绝文案"`) {
		t.Fatalf("content_preview 缺失: %s", encoded)
	}
}

// TestHealthResponseWireShape 锁定契约 §1 的健康检查字段。
func TestHealthResponseWireShape(t *testing.T) {
	encoded, err := json.Marshal(HealthResponse{Status: "ok", Version: Version, PID: 1234})
	if err != nil {
		t.Fatalf("编码失败: %v", err)
	}
	want := fmt.Sprintf(`{"status":"ok","version":%q,"pid":1234}`, Version)
	if string(encoded) != want {
		t.Fatalf("health = %s", encoded)
	}
}

// TestVersionMajorIsZero Python 侧只接受主版本 0（契约 §8）。
func TestVersionMajorIsZero(t *testing.T) {
	if !strings.HasPrefix(Version, "0.") {
		t.Fatalf("version = %q，主版本必须为 0", Version)
	}
}

// TestVersionTracksContract 锁定契约 §9 变更记录里的侧车版本号。
//
// 每次按 CONTRACT.md 升版本时，这里跟着改——它就是「代码版本」与「契约版本」
// 必须同步的那道闸。
func TestVersionTracksContract(t *testing.T) {
	if Version != "0.2.0" {
		t.Fatalf("version = %q，与契约 §9（1.2 → 侧车 0.2.0）不一致", Version)
	}
}

// TestMessagePassesThroughUnknownFields 覆盖消息透传不会丢字段。
func TestMessagePassesThroughUnknownFields(t *testing.T) {
	message := Message{
		"role":         json.RawMessage(`"assistant"`),
		"content":      json.RawMessage(`null`),
		"tool_calls":   json.RawMessage(`[{"id":"call_1","type":"function","function":{"name":"f","arguments":"{}"}}]`),
		"tool_call_id": json.RawMessage(`"call_1"`),
	}
	encoded, err := json.Marshal(message)
	if err != nil {
		t.Fatalf("编码失败: %v", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatalf("解码失败: %v", err)
	}
	if string(fields["content"]) != "null" {
		t.Fatalf("content = %s", fields["content"])
	}
	if !strings.Contains(string(fields["tool_calls"]), "call_1") {
		t.Fatalf("tool_calls 丢失: %s", fields["tool_calls"])
	}
	if string(fields["tool_call_id"]) != `"call_1"` {
		t.Fatalf("tool_call_id = %s", fields["tool_call_id"])
	}
}
