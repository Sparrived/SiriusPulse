package upstream

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// pngMagicBytes 是带 PNG 魔数的字节，足够让魔数嗅探认出格式。
var pngMagicBytes = []byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x01}

// jpegMagicBytes 是带 JPEG 魔数的字节。
var jpegMagicBytes = []byte{0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10}

// newImageRequest 构造一次生图请求。
func newImageRequest(baseURL string, timeoutSeconds float64, references ...contract.ImageReference) *contract.GenerateImageRequest {
	return &contract.GenerateImageRequest{
		Upstream: contract.Upstream{
			BaseURL:        baseURL,
			APIKey:         "amkr_ik_test",
			Workspace:      "sirius-pulse/yuebai",
			TimeoutSeconds: timeoutSeconds,
		},
		Request: contract.ImageGenerationRequest{
			Model:           "person_image",
			Prompt:          "画一张表情包",
			ReferenceImages: references,
		},
	}
}

// base64Reference 把原始字节包成契约里的参考图。
func base64Reference(filename string, data []byte) contract.ImageReference {
	return contract.ImageReference{
		Filename:   filename,
		DataBase64: base64.StdEncoding.EncodeToString(data),
	}
}

// writeImageJSON 写出 200 的生图响应体。
func writeImageJSON(t *testing.T, writer http.ResponseWriter, body string) {
	t.Helper()
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusOK)
	if _, err := writer.Write([]byte(body)); err != nil {
		t.Errorf("写响应失败: %v", err)
	}
}

// b64SuccessBody 构造 data[0].b64_json 的成功响应。
func b64SuccessBody(data []byte) string {
	return fmt.Sprintf(`{"data":[{"b64_json":%q}]}`, base64.StdEncoding.EncodeToString(data))
}

// TestGenerateImageGenerationsEndpoint 覆盖契约 §5.1.2.2/§5.1.2.4/§5.1.2.5 的无参考图分支。
func TestGenerateImageGenerationsEndpoint(t *testing.T) {
	type capturedRequest struct {
		method        string
		path          string
		authorization string
		workspace     string
		contentType   string
		userAgent     string
		body          map[string]json.RawMessage
	}
	var captured capturedRequest
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		captured.method = request.Method
		captured.path = request.URL.Path
		captured.authorization = request.Header.Get("Authorization")
		captured.workspace = request.Header.Get("X-AMKR-Workspace")
		captured.contentType = request.Header.Get("Content-Type")
		captured.userAgent = request.Header.Get("User-Agent")
		raw, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("读取请求体失败: %v", err)
		}
		if err := json.Unmarshal(raw, &captured.body); err != nil {
			t.Errorf("请求体不是 JSON: %v", err)
		}
		writeImageJSON(t, writer, b64SuccessBody(pngMagicBytes))
	}))
	defer upstreamServer.Close()

	result, err := NewClient().GenerateImage(context.Background(), newImageRequest(upstreamServer.URL+"/", 5))
	if err != nil {
		t.Fatalf("调用失败: %v", err)
	}

	if captured.method != http.MethodPost {
		t.Fatalf("方法 = %q", captured.method)
	}
	if captured.path != "/v1/images/generations" {
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
	if captured.userAgent != imageRequestUserAgent {
		t.Fatalf("User-Agent = %q，图像请求必须带浏览器 UA", captured.userAgent)
	}
	if got := stringField(captured.body["model"]); got != "person_image" {
		t.Fatalf("model = %q", got)
	}
	if got := stringField(captured.body["prompt"]); got != "画一张表情包" {
		t.Fatalf("prompt = %q", got)
	}
	if result.Endpoint != contract.EndpointGenerations {
		t.Fatalf("endpoint = %q", result.Endpoint)
	}
	if result.Downloaded {
		t.Fatal("内联 b64_json 不应标记为下载")
	}
	if result.MimeType != "image/png" {
		t.Fatalf("mime_type = %q", result.MimeType)
	}
	if !bytes.Equal(result.ImageBytes, pngMagicBytes) {
		t.Fatalf("图片字节 = %v", result.ImageBytes)
	}
}

// TestGenerateImageEditsUsesMultipart 覆盖契约 §5.1.2.3 的「先发标准 multipart 表单」分支。
func TestGenerateImageEditsUsesMultipart(t *testing.T) {
	var capturedPath string
	var capturedUserAgent string
	var capturedModel string
	var capturedPrompt string
	var fileNames []string
	var fileContentTypes []string
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		capturedPath = request.URL.Path
		capturedUserAgent = request.Header.Get("User-Agent")
		if err := request.ParseMultipartForm(1 << 20); err != nil {
			t.Errorf("请求不是 multipart 表单: %v", err)
			writer.WriteHeader(http.StatusBadRequest)
			return
		}
		capturedModel = request.FormValue("model")
		capturedPrompt = request.FormValue("prompt")
		if len(request.MultipartForm.File["image[]"]) != 0 {
			t.Errorf("参考图不得使用 image[] 字段，本地上游只认 image")
		}
		for _, header := range request.MultipartForm.File["image"] {
			fileNames = append(fileNames, header.Filename)
			fileContentTypes = append(fileContentTypes, header.Header.Get("Content-Type"))
		}
		writeImageJSON(t, writer, b64SuccessBody(pngMagicBytes))
	}))
	defer upstreamServer.Close()

	references := []contract.ImageReference{
		base64Reference("sticker.png", pngMagicBytes),
		base64Reference("face.jpg", jpegMagicBytes),
		// 魔数与后缀都认不出：按契约 §5.1.2.7 兜底 image/png。
		base64Reference("blob.bin", []byte{0x00, 0x01, 0x02}),
	}
	result, err := NewClient().GenerateImage(context.Background(), newImageRequest(upstreamServer.URL, 5, references...))
	if err != nil {
		t.Fatalf("调用失败: %v", err)
	}

	if capturedPath != "/v1/images/edits" {
		t.Fatalf("上游路径 = %q", capturedPath)
	}
	if capturedUserAgent != imageRequestUserAgent {
		t.Fatalf("User-Agent = %q", capturedUserAgent)
	}
	if capturedModel != "person_image" || capturedPrompt != "画一张表情包" {
		t.Fatalf("表单字段 model=%q prompt=%q", capturedModel, capturedPrompt)
	}
	wantNames := []string{"sticker.png", "face.jpg", "blob.bin"}
	if len(fileNames) != len(wantNames) {
		t.Fatalf("文件字段数 = %d（%v），期望 %d 个重复的 image 字段", len(fileNames), fileNames, len(wantNames))
	}
	for index, want := range wantNames {
		if fileNames[index] != want {
			t.Fatalf("第 %d 个文件名 = %q，期望 %q", index, fileNames[index], want)
		}
	}
	wantTypes := []string{"image/png", "image/jpeg", "image/png"}
	for index, want := range wantTypes {
		if fileContentTypes[index] != want {
			t.Fatalf("第 %d 个文件 Content-Type = %q，期望 %q", index, fileContentTypes[index], want)
		}
	}
	if result.Endpoint != contract.EndpointEdits {
		t.Fatalf("endpoint = %q", result.Endpoint)
	}
}

// TestGenerateImageEditsFallsBackToJSON 覆盖契约 §5.1.2.3 的「4xx 回退 JSON」分支。
func TestGenerateImageEditsFallsBackToJSON(t *testing.T) {
	var attempts atomic.Int32
	var fallbackContentType string
	var fallbackBody map[string]json.RawMessage
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if attempts.Add(1) == 1 {
			if !strings.HasPrefix(request.Header.Get("Content-Type"), "multipart/form-data") {
				t.Errorf("第一次请求应为 multipart，实际 %q", request.Header.Get("Content-Type"))
			}
			writer.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = writer.Write([]byte(`{"error":"prompt missing"}`))
			return
		}
		fallbackContentType = request.Header.Get("Content-Type")
		raw, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("读取回退请求体失败: %v", err)
		}
		if err := json.Unmarshal(raw, &fallbackBody); err != nil {
			t.Errorf("回退请求体不是 JSON: %v", err)
		}
		writeImageJSON(t, writer, b64SuccessBody(jpegMagicBytes))
	}))
	defer upstreamServer.Close()

	result, err := NewClient().GenerateImage(context.Background(),
		newImageRequest(upstreamServer.URL, 5, base64Reference("sticker.png", pngMagicBytes)))
	if err != nil {
		t.Fatalf("调用失败: %v", err)
	}

	if attempts.Load() != 2 {
		t.Fatalf("请求次数 = %d，期望先表单后 JSON 共两次", attempts.Load())
	}
	if fallbackContentType != "application/json" {
		t.Fatalf("回退请求 Content-Type = %q", fallbackContentType)
	}
	if got := stringField(fallbackBody["model"]); got != "person_image" {
		t.Fatalf("回退 model = %q", got)
	}
	var images []map[string]string
	if err := json.Unmarshal(fallbackBody["images"], &images); err != nil {
		t.Fatalf("回退 images 字段无效: %v", err)
	}
	wantURL := "data:image/png;base64," + base64.StdEncoding.EncodeToString(pngMagicBytes)
	if len(images) != 1 || images[0]["image_url"] != wantURL {
		t.Fatalf("回退 images = %v，期望 %q", images, wantURL)
	}
	if result.Endpoint != contract.EndpointEditsJSON {
		t.Fatalf("endpoint = %q", result.Endpoint)
	}
	if result.MimeType != "image/jpeg" {
		t.Fatalf("mime_type = %q", result.MimeType)
	}
}

// TestGenerateImageEditsNoFallbackForCredentialAndTimeoutStatuses 覆盖契约 §5.1.2.3 的不回退集合。
func TestGenerateImageEditsNoFallbackForCredentialAndTimeoutStatuses(t *testing.T) {
	cases := []struct {
		name      string
		status    int
		wantCode  string
		retryable bool
	}{
		{"凭据 401", http.StatusUnauthorized, contract.CodeProviderAuthError, false},
		{"凭据 403", http.StatusForbidden, contract.CodeProviderAuthError, false},
		{"超时 408", http.StatusRequestTimeout, contract.CodeProviderResponseError, false},
		// 429 虽然不重发另一种形态，但按契约 §6 的 is_retryable 规则仍可重试。
		{"限流 429", http.StatusTooManyRequests, contract.CodeProviderResponseError, true},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			var attempts atomic.Int32
			upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				attempts.Add(1)
				writer.WriteHeader(testCase.status)
				_, _ = writer.Write([]byte("form rejected"))
			}))
			defer upstreamServer.Close()

			_, err := NewClient().GenerateImage(context.Background(),
				newImageRequest(upstreamServer.URL, 5, base64Reference("sticker.png", pngMagicBytes)))
			upstreamErr := asError(t, err)

			if attempts.Load() != 1 {
				t.Fatalf("请求次数 = %d，凭据/超时类 4xx 不得回退", attempts.Load())
			}
			if upstreamErr.Code != testCase.wantCode {
				t.Fatalf("error_code = %q", upstreamErr.Code)
			}
			wantMessage := fmt.Sprintf("生图接口 HTTP 错误 %d：form rejected", testCase.status)
			if upstreamErr.Message != wantMessage {
				t.Fatalf("message = %q，期望 %q", upstreamErr.Message, wantMessage)
			}
			if upstreamErr.UpstreamStatus == nil || *upstreamErr.UpstreamStatus != testCase.status {
				t.Fatalf("upstream_status = %v", upstreamErr.UpstreamStatus)
			}
			if upstreamErr.Retryable != testCase.retryable {
				t.Fatalf("is_retryable = %t，期望 %t（契约 §6）", upstreamErr.Retryable, testCase.retryable)
			}
		})
	}
}

// TestGenerateImageDownloadsURL 覆盖契约 §5.1.2.4/§5.1.2.6 的 url 下载分支。
func TestGenerateImageDownloadsURL(t *testing.T) {
	var downloadPath string
	var downloadUserAgent string
	var imageServer *httptest.Server
	imageServer = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method == http.MethodGet {
			downloadPath = request.URL.Path
			downloadUserAgent = request.Header.Get("User-Agent")
			writer.Header().Set("Content-Type", "image/png")
			_, _ = writer.Write(jpegMagicBytes)
			return
		}
		// 后缀写 .png，内容其实是 JPEG：mime 必须以魔数为准。
		writeImageJSON(t, writer, fmt.Sprintf(`{"data":[{"url":%q}]}`, imageServer.URL+"/files/generated.png"))
	}))
	defer imageServer.Close()

	result, err := NewClient().GenerateImage(context.Background(), newImageRequest(imageServer.URL, 5))
	if err != nil {
		t.Fatalf("调用失败: %v", err)
	}

	if downloadPath != "/files/generated.png" {
		t.Fatalf("下载路径 = %q", downloadPath)
	}
	if downloadUserAgent != imageRequestUserAgent {
		t.Fatalf("下载 User-Agent = %q，下载生成图片同样要带浏览器 UA", downloadUserAgent)
	}
	if !result.Downloaded {
		t.Fatal("url 分支应标记 downloaded")
	}
	if result.Endpoint != contract.EndpointGenerations {
		t.Fatalf("endpoint = %q", result.Endpoint)
	}
	if result.MimeType != "image/jpeg" {
		t.Fatalf("mime_type = %q，魔数应压过后缀", result.MimeType)
	}
	if !bytes.Equal(result.ImageBytes, jpegMagicBytes) {
		t.Fatalf("图片字节 = %v", result.ImageBytes)
	}
}

// TestGenerateImageDownloadFailures 覆盖契约 §5.1.4 的下载失败两行。
func TestGenerateImageDownloadFailures(t *testing.T) {
	cases := []struct {
		name       string
		status     int
		body       []byte
		wantMessag func(url string) string
		retryable  bool
	}{
		{
			name:   "下载 404",
			status: http.StatusNotFound,
			body:   []byte("nope"),
			wantMessag: func(url string) string {
				return fmt.Sprintf("下载生成图片失败，HTTP 404：%s", url)
			},
			retryable: false,
		},
		{
			name:   "下载 500",
			status: http.StatusInternalServerError,
			body:   []byte("boom"),
			wantMessag: func(url string) string {
				return fmt.Sprintf("下载生成图片失败，HTTP 500：%s", url)
			},
			retryable: true,
		},
		{
			name:   "下载得到空内容",
			status: http.StatusOK,
			body:   nil,
			wantMessag: func(url string) string {
				return fmt.Sprintf("下载生成图片得到空内容：%s", url)
			},
			retryable: true,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			var imageServer *httptest.Server
			imageServer = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				if request.Method == http.MethodGet {
					writer.WriteHeader(testCase.status)
					_, _ = writer.Write(testCase.body)
					return
				}
				writeImageJSON(t, writer, fmt.Sprintf(`{"data":[{"url":%q}]}`, imageServer.URL+"/files/a.png"))
			}))
			defer imageServer.Close()

			_, err := NewClient().GenerateImage(context.Background(), newImageRequest(imageServer.URL, 5))
			upstreamErr := asError(t, err)
			if upstreamErr.Code != contract.CodeProviderResponseError {
				t.Fatalf("error_code = %q", upstreamErr.Code)
			}
			wantMessage := testCase.wantMessag(imageServer.URL + "/files/a.png")
			if upstreamErr.Message != wantMessage {
				t.Fatalf("message = %q，期望 %q", upstreamErr.Message, wantMessage)
			}
			if upstreamErr.Retryable != testCase.retryable {
				t.Fatalf("is_retryable = %t，期望 %t", upstreamErr.Retryable, testCase.retryable)
			}
		})
	}
}

// TestGenerateImageResponseErrors 覆盖契约 §5.1.4 的响应结构错误文案。
func TestGenerateImageResponseErrors(t *testing.T) {
	cases := []struct {
		name        string
		body        string
		wantMessage string
		wantPrefix  string
	}{
		{"不是合法 JSON", "not json", "生图接口返回的不是合法 JSON：not json", ""},
		{"顶层是数组", `[]`, "生图接口返回结构异常：[]", ""},
		{"顶层是字符串", `"x"`, `生图接口返回结构异常："x"`, ""},
		{"缺 data", `{}`, "生图接口没有返回任何图片数据。", ""},
		{"data 是空数组", `{"data":[]}`, "生图接口没有返回任何图片数据。", ""},
		{"data 是 null", `{"data":null}`, "生图接口没有返回任何图片数据。", ""},
		{"data 不是数组", `{"data":{"b64_json":"aGk="}}`, "生图接口没有返回任何图片数据。", ""},
		{"data[0] 不是对象", `{"data":["x"]}`, "生图接口没有返回任何图片数据。", ""},
		{"data[0] 是 null", `{"data":[null]}`, "生图接口没有返回任何图片数据。", ""},
		{"b64_json 无法解码", `{"data":[{"b64_json":"不是 base64"}]}`, "", "生图接口返回的 b64_json 无法解码："},
		// 尾部换行会被 strip 掉，所以用内部换行验证「拒绝换行」这条守卫。
		{"b64_json 含换行", `{"data":[{"b64_json":"iVBORw0\nKGgo="}]}`, "生图接口返回的 b64_json 无法解码：base64 数据中含换行", ""},
		{"b64_json 是空串", `{"data":[{"b64_json":""}]}`, "生图接口既没有 b64_json 也没有 url。", ""},
		{"既无 b64 也无 url", `{"data":[{"revised_prompt":"x"}]}`, "生图接口既没有 b64_json 也没有 url。", ""},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writeImageJSON(t, writer, testCase.body)
			}))
			defer upstreamServer.Close()

			_, err := NewClient().GenerateImage(context.Background(), newImageRequest(upstreamServer.URL, 5))
			upstreamErr := asError(t, err)
			if upstreamErr.Code != contract.CodeProviderResponseError {
				t.Fatalf("error_code = %q", upstreamErr.Code)
			}
			if upstreamErr.HTTPStatus != contract.StatusProviderFailure {
				t.Fatalf("http_status = %d", upstreamErr.HTTPStatus)
			}
			if upstreamErr.UpstreamStatus != nil {
				t.Fatalf("本地判定的失败不应带 upstream_status：%v", *upstreamErr.UpstreamStatus)
			}
			if !upstreamErr.Retryable {
				t.Fatal("无状态码的本地判定按契约 §6 应可重试")
			}
			if testCase.wantMessage != "" && upstreamErr.Message != testCase.wantMessage {
				t.Fatalf("message = %q，期望 %q", upstreamErr.Message, testCase.wantMessage)
			}
			if testCase.wantPrefix != "" && !strings.HasPrefix(upstreamErr.Message, testCase.wantPrefix) {
				t.Fatalf("message = %q，期望前缀 %q", upstreamErr.Message, testCase.wantPrefix)
			}
		})
	}
}

// TestGenerateImageMapsUpstreamStatus 覆盖契约 §5.1.4 的上游状态码错误行。
func TestGenerateImageMapsUpstreamStatus(t *testing.T) {
	cases := []struct {
		name      string
		status    int
		wantCode  string
		retryable bool
	}{
		{"400", http.StatusBadRequest, contract.CodeProviderResponseError, false},
		{"401", http.StatusUnauthorized, contract.CodeProviderAuthError, false},
		{"403", http.StatusForbidden, contract.CodeProviderAuthError, false},
		{"500", http.StatusInternalServerError, contract.CodeProviderResponseError, true},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.WriteHeader(testCase.status)
				_, _ = writer.Write([]byte(`{"error":"boom"}`))
			}))
			defer upstreamServer.Close()

			_, err := NewClient().GenerateImage(context.Background(), newImageRequest(upstreamServer.URL, 5))
			upstreamErr := asError(t, err)
			if upstreamErr.Code != testCase.wantCode {
				t.Fatalf("error_code = %q，期望 %q", upstreamErr.Code, testCase.wantCode)
			}
			if upstreamErr.HTTPStatus != contract.StatusProviderFailure {
				t.Fatalf("http_status = %d", upstreamErr.HTTPStatus)
			}
			if upstreamErr.UpstreamStatus == nil || *upstreamErr.UpstreamStatus != testCase.status {
				t.Fatalf("upstream_status = %v", upstreamErr.UpstreamStatus)
			}
			if upstreamErr.Retryable != testCase.retryable {
				t.Fatalf("is_retryable = %t，期望 %t", upstreamErr.Retryable, testCase.retryable)
			}
			wantMessage := fmt.Sprintf("生图接口 HTTP 错误 %d：%s", testCase.status, `{"error":"boom"}`)
			if upstreamErr.Message != wantMessage {
				t.Fatalf("message = %q，期望 %q", upstreamErr.Message, wantMessage)
			}
		})
	}
}

// TestGenerateImageTotalTimeout 覆盖契约 §5.1.2.1：timeout_seconds 是整请求总超时。
func TestGenerateImageTotalTimeout(t *testing.T) {
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		select {
		case <-request.Context().Done():
		case <-time.After(time.Second):
		}
		_, _ = writer.Write([]byte(`{}`))
	}))
	defer upstreamServer.Close()

	_, err := NewClient().GenerateImage(context.Background(), newImageRequest(upstreamServer.URL, 0.05))
	upstreamErr := asError(t, err)
	if upstreamErr.Code != contract.CodeUpstreamTimeout {
		t.Fatalf("error_code = %q，期望 %q", upstreamErr.Code, contract.CodeUpstreamTimeout)
	}
	if upstreamErr.HTTPStatus != contract.StatusUpstreamTimeout {
		t.Fatalf("http_status = %d", upstreamErr.HTTPStatus)
	}
	if !upstreamErr.Retryable {
		t.Fatal("超时应可重试")
	}
	if !strings.HasPrefix(upstreamErr.Message, "生图请求超过") {
		t.Fatalf("message = %q", upstreamErr.Message)
	}
}

// TestGenerateImageTimeoutCoversDownload 覆盖契约 §5.1.2.1：总超时把 url 下载也算进去。
func TestGenerateImageTimeoutCoversDownload(t *testing.T) {
	var imageServer *httptest.Server
	imageServer = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method == http.MethodGet {
			// 先给头和半个字节，再把连接吊住：只有「总超时」才能结束这次下载。
			writer.Header().Set("Content-Type", "image/png")
			writer.WriteHeader(http.StatusOK)
			_, _ = writer.Write(pngMagicBytes[:4])
			if flusher, ok := writer.(http.Flusher); ok {
				flusher.Flush()
			}
			select {
			case <-request.Context().Done():
			case <-time.After(time.Second):
			}
			return
		}
		writeImageJSON(t, writer, fmt.Sprintf(`{"data":[{"url":%q}]}`, imageServer.URL+"/files/slow.png"))
	}))
	defer imageServer.Close()

	_, err := NewClient().GenerateImage(context.Background(), newImageRequest(imageServer.URL, 0.2))
	upstreamErr := asError(t, err)
	if upstreamErr.Code != contract.CodeUpstreamTimeout {
		t.Fatalf("error_code = %q，期望 %q（下载也在总超时之内）", upstreamErr.Code, contract.CodeUpstreamTimeout)
	}
}

// TestGenerateImageConnectionError 覆盖契约 §6 的 PROVIDER_CONNECTION_ERROR。
func TestGenerateImageConnectionError(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("监听失败: %v", err)
	}
	baseURL := "http://" + listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatalf("关闭监听失败: %v", err)
	}

	_, callErr := NewClient().GenerateImage(context.Background(), newImageRequest(baseURL, 5))
	upstreamErr := asError(t, callErr)
	if upstreamErr.Code != contract.CodeProviderConnectionError {
		t.Fatalf("error_code = %q", upstreamErr.Code)
	}
	if !upstreamErr.Retryable {
		t.Fatal("连不上上游应可重试")
	}
	if !strings.HasPrefix(upstreamErr.Message, "生图请求异常：") {
		t.Fatalf("message = %q", upstreamErr.Message)
	}
}

// TestGenerateImageRejectsBadReferenceBase64 覆盖 sidecar 自身的入参校验。
func TestGenerateImageRejectsBadReferenceBase64(t *testing.T) {
	request := newImageRequest("http://127.0.0.1:1", 5, contract.ImageReference{
		Filename:   "sticker.png",
		DataBase64: "不是 base64",
	})
	_, err := NewClient().GenerateImage(context.Background(), request)
	upstreamErr := asError(t, err)
	if upstreamErr.Code != contract.CodeInvalidRequest {
		t.Fatalf("error_code = %q", upstreamErr.Code)
	}
	if upstreamErr.HTTPStatus != contract.StatusInvalidRequest {
		t.Fatalf("http_status = %d", upstreamErr.HTTPStatus)
	}
}

// TestGenerateImageMimeFallsBackToSuffix 覆盖契约 §5.1.2.7 的「魔数 → 后缀 → image/png」次序。
func TestGenerateImageMimeFallsBackToSuffix(t *testing.T) {
	unknownBytes := []byte{0x00, 0x01, 0x02, 0x03}

	t.Run("下载 url 带后缀与签名", func(t *testing.T) {
		var imageServer *httptest.Server
		imageServer = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			if request.Method == http.MethodGet {
				_, _ = writer.Write(unknownBytes)
				return
			}
			writeImageJSON(t, writer, fmt.Sprintf(`{"data":[{"url":%q}]}`, imageServer.URL+"/files/a.webp?sig=abc"))
		}))
		defer imageServer.Close()

		result, err := NewClient().GenerateImage(context.Background(), newImageRequest(imageServer.URL, 5))
		if err != nil {
			t.Fatalf("调用失败: %v", err)
		}
		if result.MimeType != "image/webp" {
			t.Fatalf("mime_type = %q，期望按后缀猜成 image/webp", result.MimeType)
		}
	})

	t.Run("内联字节认不出兜底 png", func(t *testing.T) {
		upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
			writeImageJSON(t, writer, b64SuccessBody(unknownBytes))
		}))
		defer upstreamServer.Close()

		result, err := NewClient().GenerateImage(context.Background(), newImageRequest(upstreamServer.URL, 5))
		if err != nil {
			t.Fatalf("调用失败: %v", err)
		}
		if result.MimeType != "image/png" {
			t.Fatalf("mime_type = %q，期望兜底 image/png", result.MimeType)
		}
	})
}

// TestDecodeBase64ImageRejectsEmptyResult 覆盖契约 §5.1.4 的「解出空字节」防护分支。
//
// 非空且合法的 base64 至少解出 1 字节，正常上游进不来这条分支；这里直接压住守卫，
// 防止它被当成死代码删掉。
func TestDecodeBase64ImageRejectsEmptyResult(t *testing.T) {
	_, err := decodeBase64Image("")
	if err == nil {
		t.Fatal("空 base64 应判为失败")
	}
	upstreamErr := asError(t, err)
	if upstreamErr.Message != "生图接口返回了空图片。" {
		t.Fatalf("message = %q", upstreamErr.Message)
	}
}

// TestChatRequestDoesNotSendImageUserAgent 锁定契约 §5.1.2.4：聊天链路不带图像链路的浏览器 UA。
func TestChatRequestDoesNotSendImageUserAgent(t *testing.T) {
	var chatUserAgent string
	upstreamServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		chatUserAgent = request.Header.Get("User-Agent")
		writeSSE(t, writer, map[string]any{
			"choices": []any{map[string]any{"delta": map[string]any{"content": "在的"}}},
		})
	}))
	defer upstreamServer.Close()

	request := &contract.GenerateRequest{
		Upstream: contract.Upstream{BaseURL: upstreamServer.URL, APIKey: "amkr_ik_test", TimeoutSeconds: 5},
		Request: contract.CompletionRequest{
			Model:        "response_generate",
			SystemPrompt: "你是月白",
			Messages:     []contract.Message{messageJSON(t, "user", "在吗")},
		},
	}
	if _, err := NewClient().Generate(context.Background(), request); err != nil {
		t.Fatalf("调用失败: %v", err)
	}
	if chatUserAgent == imageRequestUserAgent {
		t.Fatal("聊天链路不得携带图像链路的浏览器 UA（契约 §5.1.2.4）")
	}
}
