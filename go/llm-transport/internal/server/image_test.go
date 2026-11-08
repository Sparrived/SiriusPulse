package server

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// imageRequestBody 构造 POST /v1/generate-image 的请求体。
func imageRequestBody(t *testing.T, baseURL string, references []contract.ImageReference) []byte {
	t.Helper()
	encoded, err := json.Marshal(contract.GenerateImageRequest{
		Upstream: contract.Upstream{
			BaseURL:        baseURL,
			APIKey:         "amkr_ik_test",
			Workspace:      "sirius-pulse/yuebai",
			TimeoutSeconds: 30,
		},
		Request: contract.ImageGenerationRequest{
			Model:           "person_image",
			Prompt:          "画一张表情包",
			ReferenceImages: references,
		},
	})
	if err != nil {
		t.Fatalf("构造请求体失败: %v", err)
	}
	return encoded
}

// postGenerateImage 发一次生图请求，返回状态码与响应体。
func postGenerateImage(t *testing.T, sidecarURL string, token string, body []byte) (int, []byte) {
	t.Helper()
	request, err := http.NewRequest(http.MethodPost, sidecarURL+"/v1/generate-image", bytes.NewReader(body))
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

// TestGenerateImageRequiresToken 覆盖契约 §1/§6：生图端点同样要 Bearer 鉴权。
func TestGenerateImageRequiresToken(t *testing.T) {
	sidecar := newSidecar(t, "secret")
	body := imageRequestBody(t, "http://127.0.0.1:1", nil)

	cases := []struct {
		name   string
		token  string
		header string
	}{
		{"没有 Authorization", "", ""},
		{"token 不匹配", "wrong", "Bearer wrong"},
		{"scheme 不对", "secret", "Token secret"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			request, err := http.NewRequest(http.MethodPost, sidecar.URL+"/v1/generate-image", bytes.NewReader(body))
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
			if errorBody.ErrorCode != contract.CodeUnauthorized || errorBody.IsRetryable {
				t.Fatalf("错误体 = %+v", errorBody)
			}
		})
	}
}

// TestGenerateImageRejectsInvalidRequest 覆盖契约 §6 的 400 行（与聊天端点同一套校验）。
func TestGenerateImageRejectsInvalidRequest(t *testing.T) {
	sidecar := newSidecar(t, "secret")

	cases := []struct {
		name        string
		body        []byte
		wantMessage string
	}{
		{
			name: "非法 JSON",
			body: []byte(`{"upstream":`),
		},
		{
			name:        "缺 base_url",
			body:        []byte(`{"upstream":{"api_key":"k","timeout_seconds":30},"request":{"model":"m","prompt":"p"}}`),
			wantMessage: "缺少 upstream.base_url。",
		},
		{
			name:        "缺 api_key",
			body:        []byte(`{"upstream":{"base_url":"http://127.0.0.1:1","timeout_seconds":30},"request":{"model":"m","prompt":"p"}}`),
			wantMessage: "缺少 upstream.api_key。",
		},
		{
			name:        "timeout 为 0",
			body:        []byte(`{"upstream":{"base_url":"http://127.0.0.1:1","api_key":"k","timeout_seconds":0},"request":{"model":"m","prompt":"p"}}`),
			wantMessage: "upstream.timeout_seconds 必须大于 0。",
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			status, raw := postGenerateImage(t, sidecar.URL, "secret", testCase.body)
			if status != http.StatusBadRequest {
				t.Fatalf("status = %d (%s)", status, raw)
			}
			errorBody := decodeErrorBody(t, raw)
			if errorBody.ErrorCode != contract.CodeInvalidRequest || errorBody.IsRetryable {
				t.Fatalf("错误体 = %+v", errorBody)
			}
			if testCase.wantMessage != "" && errorBody.Message != testCase.wantMessage {
				t.Fatalf("message = %q，期望 %q", errorBody.Message, testCase.wantMessage)
			}
		})
	}
}

// TestGenerateImageSuccess 覆盖契约 §5.1.3 的响应形态。
func TestGenerateImageSuccess(t *testing.T) {
	imageBytes := []byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x01}
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/v1/images/generations" {
			t.Errorf("上游路径 = %q", request.URL.Path)
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"data":[{"b64_json":"` + base64.StdEncoding.EncodeToString(imageBytes) + `"}]}`))
	}))
	defer upstreamServer.Close()

	sidecar := newSidecar(t, "secret")
	status, raw := postGenerateImage(t, sidecar.URL, "secret", imageRequestBody(t, upstreamServer.URL, nil))
	if status != http.StatusOK {
		t.Fatalf("status = %d (%s)", status, raw)
	}

	var payload contract.GenerateImageResponse
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("响应不是契约结构: %v (%s)", err, raw)
	}
	decoded, err := base64.StdEncoding.DecodeString(payload.ImageBase64)
	if err != nil {
		t.Fatalf("image_base64 不是合法 base64: %v", err)
	}
	if !bytes.Equal(decoded, imageBytes) {
		t.Fatalf("图片字节 = %v，期望 %v", decoded, imageBytes)
	}
	if payload.MimeType != "image/png" {
		t.Fatalf("mime_type = %q", payload.MimeType)
	}
	if payload.Transport.Endpoint != contract.EndpointGenerations {
		t.Fatalf("endpoint = %q", payload.Transport.Endpoint)
	}
	if payload.Transport.Downloaded {
		t.Fatal("内联 b64_json 不应标记为下载")
	}
	if payload.Transport.UpstreamMs < 0 {
		t.Fatalf("upstream_ms = %f", payload.Transport.UpstreamMs)
	}
}

// TestGenerateImageEditsRouteReachesUpstream 覆盖 §5.1.2.3 在 HTTP 层的最小闭环。
func TestGenerateImageEditsRouteReachesUpstream(t *testing.T) {
	var attempts int
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		attempts++
		if request.URL.Path != "/v1/images/edits" {
			t.Errorf("上游路径 = %q", request.URL.Path)
		}
		if attempts == 1 {
			writer.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = writer.Write([]byte(`{"error":"requires an image model"}`))
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"data":[{"b64_json":"iVBORw0KGgo="}]}`))
	}))
	defer upstreamServer.Close()

	sidecar := newSidecar(t, "secret")
	references := []contract.ImageReference{{
		Filename:   "sticker.png",
		DataBase64: base64.StdEncoding.EncodeToString([]byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a}),
	}}
	status, raw := postGenerateImage(t, sidecar.URL, "secret", imageRequestBody(t, upstreamServer.URL, references))
	if status != http.StatusOK {
		t.Fatalf("status = %d (%s)", status, raw)
	}

	var payload contract.GenerateImageResponse
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("响应不是契约结构: %v (%s)", err, raw)
	}
	if payload.Transport.Endpoint != contract.EndpointEditsJSON {
		t.Fatalf("endpoint = %q，期望回退后的 edits-json", payload.Transport.Endpoint)
	}
	if attempts != 2 {
		t.Fatalf("上游请求次数 = %d，期望先 multipart 后 JSON", attempts)
	}
}
