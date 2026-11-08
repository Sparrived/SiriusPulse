package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// rawMessage 把任意值编成 JSON 字段值。
func rawMessage(t *testing.T, value any) json.RawMessage {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("构造 JSON 失败: %v", err)
	}
	return encoded
}

// messageJSON 构造一条契约消息。
func messageJSON(t *testing.T, role string, content any) contract.Message {
	t.Helper()
	return contract.Message{
		"role":    json.RawMessage(strconv.Quote(role)),
		"content": rawMessage(t, content),
	}
}

// writeSSE 写出若干 SSE 帧并 flush。
func writeSSE(t *testing.T, writer http.ResponseWriter, frames ...map[string]any) {
	t.Helper()
	writer.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	writer.WriteHeader(http.StatusOK)
	flusher, ok := writer.(http.Flusher)
	if !ok {
		t.Fatal("响应写入器不支持 flush")
	}
	for _, frame := range frames {
		encoded, err := json.Marshal(frame)
		if err != nil {
			t.Fatalf("构造 SSE 帧失败: %v", err)
		}
		fmt.Fprintf(writer, "data: %s\n\n", encoded)
	}
	flusher.Flush()
}

// asError 把失败收敛成 *Error 并断言成功。
func asError(t *testing.T, err error) *Error {
	t.Helper()
	var upstreamErr *Error
	if !errors.As(err, &upstreamErr) {
		t.Fatalf("期望 *upstream.Error，实际 %T: %v", err, err)
	}
	return upstreamErr
}

// TestGenerateSendsStreamingPayloadAndHeaders 锁定契约 §2/§4.1 的请求形态。
func TestGenerateSendsStreamingPayloadAndHeaders(t *testing.T) {
	type capturedRequest struct {
		path          string
		authorization string
		workspace     string
		contentType   string
		body          map[string]json.RawMessage
	}
	var captured capturedRequest
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		captured.path = request.URL.Path
		captured.authorization = request.Header.Get("Authorization")
		captured.workspace = request.Header.Get("X-AMKR-Workspace")
		captured.contentType = request.Header.Get("Content-Type")
		raw, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("读取请求体失败: %v", err)
		}
		if err := json.Unmarshal(raw, &captured.body); err != nil {
			t.Errorf("请求体不是 JSON: %v", err)
		}
		writeSSE(t, writer,
			map[string]any{"choices": []any{map[string]any{"delta": map[string]any{"content": "在的"}}}},
			map[string]any{"choices": []any{map[string]any{
				"delta": map[string]any{"content": "。"}, "finish_reason": "stop",
			}}},
			map[string]any{"choices": []any{}, "usage": map[string]any{"total_tokens": 17}},
		)
	}))
	defer upstreamServer.Close()

	request := &contract.GenerateRequest{
		Upstream: contract.Upstream{
			BaseURL:        upstreamServer.URL + "/", // 末尾斜杠由 sidecar 归一
			APIKey:         "amkr_ik_test",
			Workspace:      "sirius-pulse/yuebai",
			TimeoutSeconds: 5,
		},
		Request: contract.CompletionRequest{
			Model:        "response_generate",
			SystemPrompt: "你是月白",
			Messages:     []contract.Message{messageJSON(t, "user", "在吗")},
			Purpose:      "response_generate",
		},
	}
	result, err := NewClient().Generate(context.Background(), request)
	if err != nil {
		t.Fatalf("调用失败: %v", err)
	}

	if captured.path != "/v1/chat/completions" {
		t.Fatalf("上游路径 = %q", captured.path)
	}
	if captured.authorization != "Bearer amkr_ik_test" {
		t.Fatalf("Authorization = %q", captured.authorization)
	}
	if captured.workspace != "sirius-pulse/yuebai" {
		t.Fatalf("X-AMKR-Workspace = %q", captured.workspace)
	}
	if captured.contentType != "application/json" {
		t.Fatalf("Content-Type = %q", captured.contentType)
	}
	if string(captured.body["stream"]) != "true" {
		t.Fatalf("stream = %s", captured.body["stream"])
	}
	if string(captured.body["stream_options"]) != `{"include_usage":true}` {
		t.Fatalf("stream_options = %s", captured.body["stream_options"])
	}
	if string(captured.body["model"]) != `"response_generate"` {
		t.Fatalf("model = %s", captured.body["model"])
	}
	for _, forbidden := range []string{"temperature", "max_tokens", "top_p", "seed"} {
		if _, exists := captured.body[forbidden]; exists {
			t.Fatalf("请求体不应含采样参数 %s", forbidden)
		}
	}
	var messages []map[string]json.RawMessage
	if err := json.Unmarshal(captured.body["messages"], &messages); err != nil {
		t.Fatalf("messages 结构异常: %v", err)
	}
	if len(messages) != 2 {
		t.Fatalf("messages 数量 = %d", len(messages))
	}
	if string(messages[0]["role"]) != `"system"` || string(messages[0]["content"]) != `"你是月白"` {
		t.Fatalf("首条消息 = %v", messages[0])
	}
	if string(messages[1]["role"]) != `"user"` || string(messages[1]["content"]) != `"在吗"` {
		t.Fatalf("第二条消息 = %v", messages[1])
	}

	if result.Content != "在的。" {
		t.Fatalf("content = %q", result.Content)
	}
	if result.Path != contract.PathSSE {
		t.Fatalf("path = %q", result.Path)
	}
	if !strings.Contains(result.ContentType, "text/event-stream") {
		t.Fatalf("content_type = %q", result.ContentType)
	}
	if string(result.Usage) != `{"total_tokens":17}` {
		t.Fatalf("usage = %s", result.Usage)
	}
	if result.UpstreamMs < 0 {
		t.Fatalf("upstream_ms = %v", result.UpstreamMs)
	}
}

// TestGenerateOmitsWorkspaceHeaderWhenEmpty 覆盖空 workspace 不发头。
func TestGenerateOmitsWorkspaceHeaderWhenEmpty(t *testing.T) {
	workspaceHeader := "unset"
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		workspaceHeader = request.Header.Get("X-AMKR-Workspace")
		writeSSE(t, writer, map[string]any{"choices": []any{map[string]any{
			"delta": map[string]any{"content": "ok"}, "finish_reason": "stop",
		}}})
	}))
	defer upstreamServer.Close()

	request := &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 5},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	}
	if _, err := NewClient().Generate(context.Background(), request); err != nil {
		t.Fatalf("调用失败: %v", err)
	}
	if workspaceHeader != "" {
		t.Fatalf("空 workspace 不应发送 X-AMKR-Workspace，实际 %q", workspaceHeader)
	}
}

// TestGenerateNonSSEJSONFallback 覆盖网关忽略 stream 时整段 JSON 的回退（契约 §4.9）。
func TestGenerateNonSSEJSONFallback(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		io.WriteString(writer, `{"choices":[{"message":{"content":"兜底成功","reasoning_content":"想过"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"total_tokens":9}}`)
	}))
	defer upstreamServer.Close()

	result, err := NewClient().Generate(context.Background(), &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 5},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	})
	if err != nil {
		t.Fatalf("调用失败: %v", err)
	}
	if result.Content != "兜底成功" || result.ReasoningContent != "想过" {
		t.Fatalf("结果 = %+v", result)
	}
	if result.Path != contract.PathJSON {
		t.Fatalf("path = %q", result.Path)
	}
	if string(result.Usage) != `{"prompt_tokens":5,"total_tokens":9}` {
		t.Fatalf("usage = %s", result.Usage)
	}
}

// TestGenerateJSONWithToolCalls 覆盖非流式路径的 tool_calls 解析。
func TestGenerateJSONWithToolCalls(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		io.WriteString(writer, `{"choices":[{"message":{"content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"天气","arguments":"{\"city\":\"淮安\"}"}}]},"finish_reason":"tool_calls"}]}`)
	}))
	defer upstreamServer.Close()

	result, err := NewClient().Generate(context.Background(), &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 5},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	})
	if err != nil {
		t.Fatalf("有 tool_calls 时应成功返回: %v", err)
	}
	if len(result.ToolCalls) != 1 || result.ToolCalls[0].FunctionName != "天气" {
		t.Fatalf("tool_calls = %+v", result.ToolCalls)
	}
	if !strings.Contains(result.ToolCalls[0].FunctionArguments, "淮安") {
		t.Fatalf("arguments = %q", result.ToolCalls[0].FunctionArguments)
	}
}

// TestGenerateHTMLInsteadOfJSON 覆盖 200 但返回 HTML 错误页（契约 §4.9）。
func TestGenerateHTMLInsteadOfJSON(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "text/html")
		io.WriteString(writer, "<html><body>502 Bad Gateway</body></html>")
	}))
	defer upstreamServer.Close()

	_, err := NewClient().Generate(context.Background(), &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 5},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	})
	upstreamErr := asError(t, err)
	if upstreamErr.Code != contract.CodeProviderResponseError {
		t.Fatalf("错误码 = %s", upstreamErr.Code)
	}
	if !strings.Contains(upstreamErr.Message, "非 JSON") {
		t.Fatalf("message = %q", upstreamErr.Message)
	}
	if upstreamErr.UpstreamStatus == nil || *upstreamErr.UpstreamStatus != 200 {
		t.Fatalf("upstream_status = %v", upstreamErr.UpstreamStatus)
	}
	if upstreamErr.Retryable {
		t.Fatal("200 的本地判定失败按 Python 语义不可重试")
	}
}

// TestGenerateMapsUpstreamStatus 覆盖契约 §6 的状态映射表。
func TestGenerateMapsUpstreamStatus(t *testing.T) {
	cases := []struct {
		status        int
		wantCode      string
		wantHTTP      int
		wantRetryable bool
	}{
		{400, contract.CodeProviderResponseError, contract.StatusProviderFailure, false},
		{401, contract.CodeProviderAuthError, contract.StatusProviderFailure, false},
		{403, contract.CodeProviderAuthError, contract.StatusProviderFailure, false},
		{422, contract.CodeProviderResponseError, contract.StatusProviderFailure, false},
		{429, contract.CodeProviderResponseError, contract.StatusProviderFailure, true},
		{500, contract.CodeProviderResponseError, contract.StatusProviderFailure, true},
		{502, contract.CodeProviderResponseError, contract.StatusProviderFailure, true},
	}
	for _, testCase := range cases {
		t.Run(strconv.Itoa(testCase.status), func(t *testing.T) {
			upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.Header().Set("Content-Type", "application/json")
				writer.WriteHeader(testCase.status)
				io.WriteString(writer, `{"error":{"message":"上游拒绝"}}`)
			}))
			defer upstreamServer.Close()

			_, err := NewClient().Generate(context.Background(), &contract.GenerateRequest{
				Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 5},
				Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
			})
			upstreamErr := asError(t, err)
			if upstreamErr.Code != testCase.wantCode {
				t.Fatalf("错误码 = %s, want %s", upstreamErr.Code, testCase.wantCode)
			}
			if upstreamErr.HTTPStatus != testCase.wantHTTP {
				t.Fatalf("http_status = %d", upstreamErr.HTTPStatus)
			}
			if upstreamErr.Retryable != testCase.wantRetryable {
				t.Fatalf("is_retryable = %v", upstreamErr.Retryable)
			}
			if upstreamErr.UpstreamStatus == nil || *upstreamErr.UpstreamStatus != testCase.status {
				t.Fatalf("upstream_status = %v", upstreamErr.UpstreamStatus)
			}
			if !strings.Contains(upstreamErr.Message, strconv.Itoa(testCase.status)) {
				t.Fatalf("message 未带上游状态: %q", upstreamErr.Message)
			}
		})
	}
}

// TestGenerateEmptyResponseSucceeds 覆盖空响应按成功返回（契约 v1.1 §4.6）。
//
// 「空响应算不算失败」是 Python 的业务语义（return_reasoning=True 只取思维链是
// 合法场景），侧车只负责如实回一份空结果。
func TestGenerateEmptyResponseSucceeds(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.WriteHeader(http.StatusOK)
		io.WriteString(writer, "data: [DONE]\n\n")
	}))
	defer upstreamServer.Close()

	result, err := NewClient().Generate(context.Background(), &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 5},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	})
	if err != nil {
		t.Fatalf("空响应不应报错: %v", err)
	}
	if result.Content != "" {
		t.Fatalf("content = %q", result.Content)
	}
	if len(result.ToolCalls) != 0 {
		t.Fatalf("tool_calls = %v", result.ToolCalls)
	}
	if result.FinishReason != "stop" {
		t.Fatalf("finish_reason = %q", result.FinishReason)
	}
	if result.Usage != nil {
		t.Fatalf("usage = %s", result.Usage)
	}
	// transport 字段照常填充。
	if result.Path != contract.PathSSE || !strings.Contains(result.ContentType, "text/event-stream") {
		t.Fatalf("transport = %q/%q", result.Path, result.ContentType)
	}
	if result.UpstreamMs < 0 {
		t.Fatalf("upstream_ms = %v", result.UpstreamMs)
	}
}

// TestGenerateEmptyResponseWithReasoningSucceeds 覆盖「只有思维链、没有正文」的合法场景。
func TestGenerateEmptyResponseWithReasoningSucceeds(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writeSSE(t, writer, map[string]any{"choices": []any{map[string]any{
			"delta":         map[string]any{"reasoning_content": "只想不写"},
			"finish_reason": "length",
		}}})
	}))
	defer upstreamServer.Close()

	result, err := NewClient().Generate(context.Background(), &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 5},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	})
	if err != nil {
		t.Fatalf("只有思维链时不应报错: %v", err)
	}
	if result.Content != "" || result.ReasoningContent != "只想不写" {
		t.Fatalf("结果 = %+v", result)
	}
	if result.FinishReason != "length" {
		t.Fatalf("finish_reason = %q", result.FinishReason)
	}
}

// TestGenerateIdleTimeoutFails 覆盖块间静默超时（契约 §4.2）。
func TestGenerateIdleTimeoutFails(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.WriteHeader(http.StatusOK)
		io.WriteString(writer, "data: {\"choices\":[{\"delta\":{\"content\":\"半句\"}}]}\n\n")
		writer.(http.Flusher).Flush()
		// 之后静默：超过 timeout_seconds 就该判定超时。
		time.Sleep(400 * time.Millisecond)
		io.WriteString(writer, "data: [DONE]\n\n")
	}))
	defer upstreamServer.Close()

	started := time.Now()
	_, err := NewClient().Generate(context.Background(), &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "k", TimeoutSeconds: 0.1},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	})
	upstreamErr := asError(t, err)
	if upstreamErr.Code != contract.CodeUpstreamTimeout {
		t.Fatalf("错误码 = %s (%s)", upstreamErr.Code, upstreamErr.Message)
	}
	if upstreamErr.HTTPStatus != contract.StatusUpstreamTimeout {
		t.Fatalf("http_status = %d", upstreamErr.HTTPStatus)
	}
	if !upstreamErr.Retryable {
		t.Fatal("超时应可重试")
	}
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Fatalf("超时判定耗时 %s，看门狗未生效", elapsed)
	}
}

// TestGenerateConnectionError 覆盖连不上上游（契约 §6）。
func TestGenerateConnectionError(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("分配端口失败: %v", err)
	}
	address := listener.Addr().String()
	_ = listener.Close()

	_, err = NewClient().Generate(context.Background(), &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: "http://" + address, APIKey: "k", TimeoutSeconds: 2},
		Request:  contract.CompletionRequest{Model: "m", Messages: []contract.Message{messageJSON(t, "user", "hi")}},
	})
	upstreamErr := asError(t, err)
	if upstreamErr.Code != contract.CodeProviderConnectionError {
		t.Fatalf("错误码 = %s", upstreamErr.Code)
	}
	if !upstreamErr.Retryable {
		t.Fatal("连接失败应可重试")
	}
}
