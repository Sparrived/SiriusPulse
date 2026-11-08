package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// newSidecar 起一个带 token 的 sidecar 测试服务。
func newSidecar(t *testing.T, token string) *httptest.Server {
	t.Helper()
	sidecar := httptest.NewServer(New(token, contract.Version, nil).Handler())
	t.Cleanup(sidecar.Close)
	return sidecar
}

// generateRequestBody 构造 POST /v1/generate 的请求体。
func generateRequestBody(t *testing.T, baseURL string, apiKey string, messages []contract.Message) []byte {
	t.Helper()
	encoded, err := json.Marshal(contract.GenerateRequest{
		Upstream: contract.Upstream{
			BaseURL:        baseURL,
			APIKey:         apiKey,
			Workspace:      "sirius-pulse/yuebai",
			TimeoutSeconds: 5,
		},
		Request: contract.CompletionRequest{
			Model:        "response_generate",
			SystemPrompt: "你是月白",
			Messages:     messages,
			Purpose:      "response_generate",
		},
	})
	if err != nil {
		t.Fatalf("构造请求体失败: %v", err)
	}
	return encoded
}

// userMessage 构造一条纯文本用户消息。
func userMessage(t *testing.T, text string) contract.Message {
	t.Helper()
	content, err := json.Marshal(text)
	if err != nil {
		t.Fatalf("构造消息失败: %v", err)
	}
	return contract.Message{"role": json.RawMessage(`"user"`), "content": content}
}

// postGenerate 发一次生成请求，返回状态码与响应体。
func postGenerate(t *testing.T, sidecarURL string, token string, body []byte) (int, []byte) {
	t.Helper()
	request, err := http.NewRequest(http.MethodPost, sidecarURL+"/v1/generate", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer func() {
		_ = response.Body.Close()
	}()
	raw, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatalf("读取响应失败: %v", err)
	}
	return response.StatusCode, raw
}

// decodeErrorBody 解析错误响应体。
func decodeErrorBody(t *testing.T, raw []byte) contract.ErrorBody {
	t.Helper()
	var payload contract.ErrorResponse
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("错误响应不是契约结构: %v (%s)", err, raw)
	}
	return payload.Error
}

// TestHealthIsAnonymous 覆盖契约 §1 的健康检查。
func TestHealthIsAnonymous(t *testing.T) {
	sidecar := newSidecar(t, "secret")
	response, err := http.Get(sidecar.URL + "/health")
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer func() {
		_ = response.Body.Close()
	}()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	var health contract.HealthResponse
	if err := json.NewDecoder(response.Body).Decode(&health); err != nil {
		t.Fatalf("响应不是契约结构: %v", err)
	}
	if health.Status != "ok" || health.Version != contract.Version || health.PID <= 0 {
		t.Fatalf("health = %+v", health)
	}
}

// TestGenerateRejectsBadToken 覆盖契约 §6 的 401 行。
func TestGenerateRejectsBadToken(t *testing.T) {
	sidecar := newSidecar(t, "secret")
	body := generateRequestBody(t, "http://127.0.0.1:1", "k", []contract.Message{userMessage(t, "hi")})

	cases := []struct {
		name   string
		token  string
		header string
	}{
		{"没有 Authorization", "", ""},
		{"token 不匹配", "wrong", "Bearer wrong"},
		{"scheme 不对", "secret", "Token secret"},
		{"空 Bearer", "secret", "Bearer "},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			request, err := http.NewRequest(http.MethodPost, sidecar.URL+"/v1/generate", bytes.NewReader(body))
			if err != nil {
				t.Fatalf("构造请求失败: %v", err)
			}
			if testCase.header != "" {
				request.Header.Set("Authorization", testCase.header)
			}
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatalf("请求失败: %v", err)
			}
			defer func() {
				_ = response.Body.Close()
			}()
			raw, _ := io.ReadAll(response.Body)
			if response.StatusCode != http.StatusUnauthorized {
				t.Fatalf("status = %d (%s)", response.StatusCode, raw)
			}
			errorBody := decodeErrorBody(t, raw)
			if errorBody.ErrorCode != contract.CodeUnauthorized {
				t.Fatalf("error_code = %s", errorBody.ErrorCode)
			}
			if errorBody.HTTPStatus != http.StatusUnauthorized || errorBody.IsRetryable {
				t.Fatalf("错误体 = %+v", errorBody)
			}
		})
	}
}

// TestGenerateRejectsInvalidRequest 覆盖契约 §6 的 400 行。
func TestGenerateRejectsInvalidRequest(t *testing.T) {
	sidecar := newSidecar(t, "secret")

	cases := []struct {
		name string
		body []byte
	}{
		{"非法 JSON", []byte(`{"upstream":`)},
		{"缺 base_url", []byte(`{"upstream":{"api_key":"k","timeout_seconds":5},"request":{"model":"m"}}`)},
		{"缺 api_key", []byte(`{"upstream":{"base_url":"http://127.0.0.1:1","timeout_seconds":5},"request":{"model":"m"}}`)},
		{"timeout 为 0", []byte(`{"upstream":{"base_url":"http://127.0.0.1:1","api_key":"k","timeout_seconds":0},"request":{"model":"m"}}`)},
		{"timeout 为负", []byte(`{"upstream":{"base_url":"http://127.0.0.1:1","api_key":"k","timeout_seconds":-3},"request":{"model":"m"}}`)},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			status, raw := postGenerate(t, sidecar.URL, "secret", testCase.body)
			if status != http.StatusBadRequest {
				t.Fatalf("status = %d (%s)", status, raw)
			}
			errorBody := decodeErrorBody(t, raw)
			if errorBody.ErrorCode != contract.CodeInvalidRequest {
				t.Fatalf("error_code = %s", errorBody.ErrorCode)
			}
			if errorBody.HTTPStatus != http.StatusBadRequest || errorBody.IsRetryable {
				t.Fatalf("错误体 = %+v", errorBody)
			}
			if errorBody.Message == "" {
				t.Fatal("缺少 message")
			}
		})
	}
}

// writeNoisePNG 写一张噪点 PNG，供内联测试使用。
func writeNoisePNG(t *testing.T, path string, width int, height int) {
	t.Helper()
	canvas := image.NewRGBA(image.Rect(0, 0, width, height))
	state := uint32(66199)
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			state = state*1664525 + 1013904223
			canvas.SetRGBA(x, y, color.RGBA{R: uint8(state >> 16), G: uint8(state >> 8), B: uint8(state), A: 0xff})
		}
	}
	file, err := os.Create(path)
	if err != nil {
		t.Fatalf("创建图片失败: %v", err)
	}
	defer func() {
		_ = file.Close()
	}()
	if err := png.Encode(file, canvas); err != nil {
		t.Fatalf("写入图片失败: %v", err)
	}
}

// TestGenerateEndToEndInlinesImagesAndAggregates 覆盖「内联 → 上游 → 聚合 → 响应」全链路。
func TestGenerateEndToEndInlinesImagesAndAggregates(t *testing.T) {
	var upstreamBody map[string]json.RawMessage
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		raw, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("读取上游请求体失败: %v", err)
		}
		if err := json.Unmarshal(raw, &upstreamBody); err != nil {
			t.Errorf("上游请求体不是 JSON: %v", err)
		}
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.WriteHeader(http.StatusOK)
		frames := []string{
			`data: {"choices":[{"delta":{"content":"我看到了"}}]}`,
			`data: {"choices":[{"delta":{"content":"。"},"finish_reason":"stop"}]}`,
			`data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}`,
		}
		for _, frame := range frames {
			fmt.Fprintf(writer, "%s\n\n", frame)
		}
		io.WriteString(writer, "data: [DONE]\n\n")
		writer.(http.Flusher).Flush()
	}))
	defer upstreamServer.Close()

	dir := t.TempDir()
	imagePath := filepath.Join(dir, "shot.png")
	writeNoisePNG(t, imagePath, 32, 24)

	parts, err := json.Marshal([]any{
		map[string]any{"type": "image_url", "image_url": map[string]any{"url": imagePath}},
		map[string]any{"type": "text", "text": "看图"},
	})
	if err != nil {
		t.Fatalf("构造 parts 失败: %v", err)
	}
	body := generateRequestBody(t, upstreamServer.URL, "amkr_ik_test", []contract.Message{
		{"role": json.RawMessage(`"user"`), "content": parts},
	})

	sidecar := newSidecar(t, "secret")
	status, raw := postGenerate(t, sidecar.URL, "secret", body)
	if status != http.StatusOK {
		t.Fatalf("status = %d (%s)", status, raw)
	}
	var payload contract.GenerateResponse
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("响应不是契约结构: %v (%s)", err, raw)
	}
	if payload.Content != "我看到了。" {
		t.Fatalf("content = %q", payload.Content)
	}
	if payload.FinishReason != "stop" {
		t.Fatalf("finish_reason = %q", payload.FinishReason)
	}
	if string(payload.Usage) != `{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}` {
		t.Fatalf("usage = %s", payload.Usage)
	}
	if payload.Transport.LocalImageConversions != 1 {
		t.Fatalf("local_image_conversions = %d", payload.Transport.LocalImageConversions)
	}
	if payload.Transport.Path != contract.PathSSE {
		t.Fatalf("path = %q", payload.Transport.Path)
	}
	if payload.Transport.UpstreamMs < 0 {
		t.Fatalf("upstream_ms = %v", payload.Transport.UpstreamMs)
	}
	if !strings.Contains(payload.Transport.ContentType, "text/event-stream") {
		t.Fatalf("content_type = %q", payload.Transport.ContentType)
	}

	// 上游收到的是 data URL，而不是本地路径。
	var messages []map[string]json.RawMessage
	if err := json.Unmarshal(upstreamBody["messages"], &messages); err != nil {
		t.Fatalf("上游 messages 结构异常: %v", err)
	}
	if len(messages) != 2 {
		t.Fatalf("上游 messages 数量 = %d", len(messages))
	}
	sentContent := string(messages[1]["content"])
	if strings.Contains(sentContent, imagePath) {
		t.Fatalf("上游仍收到本地路径: %s", sentContent)
	}
	if !strings.Contains(sentContent, "data:image/png;base64,") {
		t.Fatalf("上游未收到 data URL: %.120s", sentContent)
	}
}

// TestGenerateMapsProviderFailures 覆盖 sidecar 把上游失败翻译成契约错误体。
func TestGenerateMapsProviderFailures(t *testing.T) {
	cases := []struct {
		name          string
		upstreamCode  int
		wantCode      string
		wantRetryable bool
	}{
		{"上游 401", 401, contract.CodeProviderAuthError, false},
		{"上游 400", 400, contract.CodeProviderResponseError, false},
		{"上游 429", 429, contract.CodeProviderResponseError, true},
		{"上游 500", 500, contract.CodeProviderResponseError, true},
	}
	sidecar := newSidecar(t, "secret")
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.Header().Set("Content-Type", "application/json")
				writer.WriteHeader(testCase.upstreamCode)
				io.WriteString(writer, `{"error":{"message":"上游拒绝"}}`)
			}))
			defer upstreamServer.Close()

			body := generateRequestBody(t, upstreamServer.URL, "k", []contract.Message{userMessage(t, "hi")})
			status, raw := postGenerate(t, sidecar.URL, "secret", body)
			if status != http.StatusBadGateway {
				t.Fatalf("status = %d (%s)", status, raw)
			}
			errorBody := decodeErrorBody(t, raw)
			if errorBody.ErrorCode != testCase.wantCode {
				t.Fatalf("error_code = %s", errorBody.ErrorCode)
			}
			if errorBody.IsRetryable != testCase.wantRetryable {
				t.Fatalf("is_retryable = %v", errorBody.IsRetryable)
			}
			if errorBody.HTTPStatus != http.StatusBadGateway {
				t.Fatalf("http_status = %d", errorBody.HTTPStatus)
			}
			if errorBody.UpstreamStatus == nil || *errorBody.UpstreamStatus != testCase.upstreamCode {
				t.Fatalf("upstream_status = %v", errorBody.UpstreamStatus)
			}
		})
	}
}

// TestGenerateContentFilteredError 覆盖内容策略拦截在响应体里带上预览。
func TestGenerateContentFilteredError(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.WriteHeader(http.StatusOK)
		io.WriteString(writer, `data: {"choices":[{"delta":{"content":"The prompt could not be submitted."}}]}`+"\n\n")
		io.WriteString(writer, "data: [DONE]\n\n")
		writer.(http.Flusher).Flush()
	}))
	defer upstreamServer.Close()

	sidecar := newSidecar(t, "secret")
	body := generateRequestBody(t, upstreamServer.URL, "k", []contract.Message{userMessage(t, "hi")})
	status, raw := postGenerate(t, sidecar.URL, "secret", body)
	if status != http.StatusBadGateway {
		t.Fatalf("status = %d (%s)", status, raw)
	}
	errorBody := decodeErrorBody(t, raw)
	if errorBody.ErrorCode != contract.CodeProviderContentFiltered {
		t.Fatalf("error_code = %s", errorBody.ErrorCode)
	}
	if errorBody.IsRetryable {
		t.Fatal("内容策略拦截不可重试")
	}
	if !strings.Contains(errorBody.ContentPreview, "The prompt could not be submitted.") {
		t.Fatalf("content_preview = %q", errorBody.ContentPreview)
	}
}
