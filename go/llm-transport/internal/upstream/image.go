package upstream

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"path"
	"strings"
	"time"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/media"
)

// 图像端点路径（契约 §5.1.2.2）。
const (
	imageGenerationsPath = "/v1/images/generations"
	imageEditsPath       = "/v1/images/edits"
)

// imageRequestUserAgent 是图像请求（生成、编辑，以及下载生成图片的 GET）固定的浏览器 UA
// （契约 §5.1.2.4）。
//
// 图像端点的上游（sub2api）套了 Cloudflare 校验，默认 UA 会被 403（error code 1010），
// 所以图像链路必须自带一个常见的浏览器 UA；聊天链路不经过这层校验，**不要**把这个值
// 带到 /v1/chat/completions。
const imageRequestUserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
	"(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

// 生图链路的兜底常量。
const (
	// defaultImageTimeoutSeconds 是 upstream.timeout_seconds 异常时的兜底总超时。
	defaultImageTimeoutSeconds = 120.0
	// defaultGeneratedImageMime 是魔数与后缀都认不出时的兜底媒体类型（契约 §5.1.2.7）。
	defaultGeneratedImageMime = "image/png"
	// defaultImageFilename 是参考图缺文件名时 multipart 文件字段的兜底名字。
	defaultImageFilename = "image.png"
)

// ImageResult 是一次成功生图在传输层解出的结果。
type ImageResult struct {
	// ImageBytes 是图片的原始字节。
	ImageBytes []byte
	// MimeType 是按魔数/后缀判定出的媒体类型。
	MimeType string
	// Endpoint 是实际生效的端点（契约 §5.1.3）。
	Endpoint string
	// Downloaded 表示这次是否走了 url 下载。
	Downloaded bool
	// UpstreamMs 是整次生图（含下载）的耗时（毫秒）。
	UpstreamMs float64
}

// referenceImage 是解码后的参考图，带推断出的媒体类型。
type referenceImage struct {
	filename string
	data     []byte
	mimeType string
}

// GenerateImage 按契约 §5.1 调用 AMKR 的图像端点并返回图片原始字节。
//
// 与 Generate 的超时语义不同：timeout_seconds 是**整次请求**（含 url 下载）的总上限，
// 不是块间静默超时（契约 §5.1.2.1），因此整个调用共用一个 deadline。
func (c *Client) GenerateImage(ctx context.Context, request *contract.GenerateImageRequest) (*ImageResult, error) {
	references, referenceErr := decodeReferenceImages(request.Request.ReferenceImages)
	if referenceErr != nil {
		return nil, referenceErr
	}
	timeout := imageTimeout(request.Upstream.TimeoutSeconds)
	requestContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	started := time.Now()
	response, endpoint, requestErr := c.postImageRequest(requestContext, &request.Upstream, &request.Request, references, timeout)
	if requestErr != nil {
		return nil, requestErr
	}
	defer func() {
		_ = response.Body.Close()
	}()

	raw, readErr := io.ReadAll(response.Body)
	if readErr != nil {
		return nil, imageRequestError(requestContext, readErr, timeout)
	}
	if response.StatusCode >= 400 {
		return nil, imageStatusError(response.StatusCode, string(raw))
	}
	body, bodyErr := parseImageBody(string(raw))
	if bodyErr != nil {
		return nil, bodyErr
	}
	result, itemErr := c.readImageItem(requestContext, body, timeout)
	if itemErr != nil {
		return nil, itemErr
	}
	result.Endpoint = endpoint
	result.UpstreamMs = elapsedMilliseconds(started)
	return result, nil
}

// postImageRequest 按契约 §5.1.2.2/§5.1.2.3 发请求，返回响应与实际生效的端点。
//
// 没有参考图走 generations（一个 JSON 请求）；有参考图先发 OpenAI 标准的 multipart
// 表单，形态不兼容的 4xx 再回退成 JSON 的 images[].image_url。
func (c *Client) postImageRequest(
	ctx context.Context,
	config *contract.Upstream,
	request *contract.ImageGenerationRequest,
	references []referenceImage,
	timeout time.Duration,
) (*http.Response, string, error) {
	baseURL := strings.TrimRight(strings.TrimSpace(config.BaseURL), "/")
	if len(references) == 0 {
		payload, marshalErr := json.Marshal(map[string]string{
			"model":  request.Model,
			"prompt": request.Prompt,
		})
		if marshalErr != nil {
			return nil, "", NewResponseError("构造生图请求体失败："+marshalErr.Error(), nil)
		}
		response, requestErr := c.doImageRequest(ctx, config, baseURL+imageGenerationsPath, bytes.NewReader(payload), "application/json", timeout)
		if requestErr != nil {
			return nil, "", requestErr
		}
		return response, contract.EndpointGenerations, nil
	}

	form, contentType, formErr := buildEditMultipart(request, references)
	if formErr != nil {
		return nil, "", NewResponseError("构造生图表单失败："+formErr.Error(), nil)
	}
	response, requestErr := c.doImageRequest(ctx, config, baseURL+imageEditsPath, form, contentType, timeout)
	if requestErr != nil {
		return nil, "", requestErr
	}
	if !shouldFallbackToEditJSON(response.StatusCode) {
		return response, contract.EndpointEdits, nil
	}

	// 表单形态被拒：同一个 deadline 内换成 JSON 形态再发一次（契约 §5.1.2.3）。
	_ = response.Body.Close()
	payload, marshalErr := buildEditJSON(request, references)
	if marshalErr != nil {
		return nil, "", NewResponseError("构造生图请求体失败："+marshalErr.Error(), nil)
	}
	fallback, fallbackErr := c.doImageRequest(ctx, config, baseURL+imageEditsPath, bytes.NewReader(payload), "application/json", timeout)
	if fallbackErr != nil {
		return nil, "", fallbackErr
	}
	return fallback, contract.EndpointEditsJSON, nil
}

// shouldFallbackToEditJSON 判断第一次 edits 的响应是否值得回退成 JSON 形态（契约 §5.1.2.3）。
//
// 只有「4xx 且不是凭据/超时类」才回退：401/403 是凭据问题，408/429 是超时与限流，
// 换个形态重发只会再烧一次配额；5xx 是上游故障，与请求形态无关。
func shouldFallbackToEditJSON(statusCode int) bool {
	if statusCode < 400 || statusCode >= 500 {
		return false
	}
	switch statusCode {
	case http.StatusUnauthorized, http.StatusForbidden, http.StatusRequestTimeout, http.StatusTooManyRequests:
		return false
	}
	return true
}

// doImageRequest 发一次图像请求：鉴权头、工作空间头与浏览器 UA 固定带上（契约 §5.1.2.4/§5.1.2.5）。
func (c *Client) doImageRequest(
	ctx context.Context,
	config *contract.Upstream,
	url string,
	body io.Reader,
	contentType string,
	timeout time.Duration,
) (*http.Response, error) {
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, url, body)
	if err != nil {
		return nil, NewConnectionError("构造生图请求失败：" + err.Error())
	}
	httpRequest.Header.Set("Content-Type", contentType)
	httpRequest.Header.Set("User-Agent", imageRequestUserAgent)
	httpRequest.Header.Set("Authorization", "Bearer "+config.APIKey)
	if workspace := strings.TrimSpace(config.Workspace); workspace != "" {
		httpRequest.Header.Set("X-AMKR-Workspace", workspace)
	}
	response, err := c.HTTPClient.Do(httpRequest)
	if err != nil {
		return nil, imageRequestError(ctx, err, timeout)
	}
	return response, nil
}

// buildEditMultipart 构造 OpenAI 标准的图像编辑表单（契约 §5.1.2.3）。
//
// 多张参考图用**重复的 image 字段**，不要用 image[]：本地上游只把 image 认作文件字段，
// image[] 会被当成「没收到图片」。
func buildEditMultipart(request *contract.ImageGenerationRequest, references []referenceImage) (*bytes.Buffer, string, error) {
	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	if err := writer.WriteField("model", request.Model); err != nil {
		return nil, "", err
	}
	if err := writer.WriteField("prompt", request.Prompt); err != nil {
		return nil, "", err
	}
	for _, reference := range references {
		header := make(textproto.MIMEHeader)
		header.Set("Content-Disposition", fmt.Sprintf("form-data; name=%q; filename=%q", "image", reference.filename))
		header.Set("Content-Type", reference.mimeType)
		part, err := writer.CreatePart(header)
		if err != nil {
			return nil, "", err
		}
		if _, err := part.Write(reference.data); err != nil {
			return nil, "", err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, "", err
	}
	return body, writer.FormDataContentType(), nil
}

// buildEditJSON 构造回退形态的 JSON 请求体：images[].image_url 用 data URL（契约 §5.1.2.3）。
func buildEditJSON(request *contract.ImageGenerationRequest, references []referenceImage) ([]byte, error) {
	images := make([]map[string]string, 0, len(references))
	for _, reference := range references {
		images = append(images, map[string]string{
			"image_url": "data:" + reference.mimeType + ";base64," + base64.StdEncoding.EncodeToString(reference.data),
		})
	}
	return json.Marshal(map[string]any{
		"model":  request.Model,
		"prompt": request.Prompt,
		"images": images,
	})
}

// decodeReferenceImages 把线上传输的 base64 参考图解成字节（契约 §5.1.1）。
func decodeReferenceImages(images []contract.ImageReference) ([]referenceImage, error) {
	if len(images) == 0 {
		return nil, nil
	}
	decoded := make([]referenceImage, 0, len(images))
	for index, image := range images {
		data, err := base64.StdEncoding.DecodeString(strings.TrimSpace(image.DataBase64))
		if err != nil {
			// Python 侧总是从内存字节现编 base64，这里失败只能是调用方构造有误；
			// 契约 §5.1.4 没有单列这种输入错误，按 §6 的 INVALID_REQUEST 报。
			return nil, NewInvalidRequestError(
				fmt.Sprintf("reference_images[%d].data_base64 不是合法 base64：%v", index, err))
		}
		filename := strings.TrimSpace(image.Filename)
		if filename == "" {
			filename = defaultImageFilename
		}
		decoded = append(decoded, referenceImage{
			filename: filename,
			data:     data,
			mimeType: guessImageMime(filename, data),
		})
	}
	return decoded, nil
}

// parseImageBody 解析生图响应体，并按契约 §5.1.4 区分「不是 JSON」与「顶层不是对象」。
func parseImageBody(raw string) (map[string]json.RawMessage, error) {
	var probe any
	if err := json.Unmarshal([]byte(raw), &probe); err != nil {
		return nil, NewResponseError("生图接口返回的不是合法 JSON："+truncateRunes(raw, 200), nil)
	}
	if _, ok := probe.(map[string]any); !ok {
		return nil, NewResponseError("生图接口返回结构异常："+truncateRunes(raw, 200), nil)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &fields); err != nil {
		return nil, NewResponseError("生图接口返回的不是合法 JSON："+truncateRunes(raw, 200), nil)
	}
	return fields, nil
}

// readImageItem 从 data[0] 取图片：优先内联 b64_json，退化时下载 url（契约 §5.1.2.6）。
func (c *Client) readImageItem(ctx context.Context, body map[string]json.RawMessage, timeout time.Duration) (*ImageResult, error) {
	item, itemErr := firstImageItem(body)
	if itemErr != nil {
		return nil, itemErr
	}
	encoded := strings.TrimSpace(stringField(item["b64_json"]))
	if encoded != "" {
		imageBytes, decodeErr := decodeBase64Image(encoded)
		if decodeErr != nil {
			return nil, decodeErr
		}
		// 内联字节没有名字可用，只按魔数判定（认不出兜底 image/png）。
		return &ImageResult{ImageBytes: imageBytes, MimeType: guessImageMime("", imageBytes)}, nil
	}
	imageURL := strings.TrimSpace(stringField(item["url"]))
	if imageURL == "" {
		return nil, NewResponseError("生图接口既没有 b64_json 也没有 url。", nil)
	}
	return c.downloadImage(ctx, imageURL, timeout)
}

// firstImageItem 取 data[0] 并断言它是对象（契约 §5.1.4 的「没有返回任何图片数据」）。
func firstImageItem(body map[string]json.RawMessage) (map[string]json.RawMessage, error) {
	var items []json.RawMessage
	if raw, ok := body["data"]; ok && !isNullJSON(raw) {
		if err := json.Unmarshal(raw, &items); err != nil {
			items = nil
		}
	}
	noImage := NewResponseError("生图接口没有返回任何图片数据。", nil)
	if len(items) == 0 {
		return nil, noImage
	}
	var item map[string]json.RawMessage
	if err := json.Unmarshal(items[0], &item); err != nil || item == nil {
		return nil, noImage
	}
	return item, nil
}

// decodeBase64Image 严格解码 b64_json（契约 §5.1.2.6）。
//
// 严格对齐 Python 的 b64decode(validate=True)：Go 的默认解码器会静默跳过 \r \n，
// 这里额外拒掉换行，避免把「被网关折行的响应」当成合法图片。
func decodeBase64Image(encoded string) ([]byte, error) {
	if strings.ContainsAny(encoded, "\r\n") {
		return nil, NewResponseError("生图接口返回的 b64_json 无法解码：base64 数据中含换行", nil)
	}
	imageBytes, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil {
		return nil, NewResponseError("生图接口返回的 b64_json 无法解码："+err.Error(), nil)
	}
	if len(imageBytes) == 0 {
		// 防护分支：非空且合法的 base64 至少解出 1 字节，正常上游进不来这里。
		return nil, NewResponseError("生图接口返回了空图片。", nil)
	}
	return imageBytes, nil
}

// downloadImage 用浏览器 UA 下载生成图片（契约 §5.1.2.4/§5.1.2.6）。
func (c *Client) downloadImage(ctx context.Context, imageURL string, timeout time.Duration) (*ImageResult, error) {
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodGet, imageURL, nil)
	if err != nil {
		return nil, NewConnectionError("构造图片下载请求失败：" + err.Error() + "（" + imageURL + "）")
	}
	httpRequest.Header.Set("User-Agent", imageRequestUserAgent)
	response, err := c.HTTPClient.Do(httpRequest)
	if err != nil {
		return nil, imageRequestError(ctx, err, timeout)
	}
	defer func() {
		_ = response.Body.Close()
	}()
	if response.StatusCode >= 400 {
		statusCode := response.StatusCode
		return nil, NewResponseError(
			fmt.Sprintf("下载生成图片失败，HTTP %d：%s", statusCode, imageURL), &statusCode)
	}
	imageBytes, readErr := io.ReadAll(response.Body)
	if readErr != nil {
		return nil, imageRequestError(ctx, readErr, timeout)
	}
	if len(imageBytes) == 0 {
		return nil, NewResponseError("下载生成图片得到空内容："+imageURL, nil)
	}
	return &ImageResult{
		ImageBytes: imageBytes,
		MimeType:   guessImageMime(imageURL, imageBytes),
		Downloaded: true,
	}, nil
}

// guessImageMime 按「魔数 → 后缀 → image/png」推定媒体类型（契约 §5.1.2.7）。
//
// 魔数优先：缓存常把各种图片统一存成 .jpg，内容却可能是 PNG，而上游按字节校验声明，
// 声明错了整个请求会被拒。name 可以是文件名或下载 url，没有就传空串。
func guessImageMime(name string, data []byte) string {
	if mime, ok := media.DetectImageMime(data); ok {
		return mime
	}
	if guessed := mimeBySuffix(name); guessed != "" {
		return guessed
	}
	return defaultGeneratedImageMime
}

// mimeBySuffix 按常见图片后缀推断媒体类型，认不出时返回空串。
func mimeBySuffix(name string) string {
	trimmed := strings.TrimSpace(name)
	if index := strings.IndexAny(trimmed, "?#"); index >= 0 {
		// 下载 url 常带短时效签名，后缀要按路径部分判断。
		trimmed = trimmed[:index]
	}
	switch strings.ToLower(path.Ext(trimmed)) {
	case ".png":
		return "image/png"
	case ".jpg", ".jpeg", ".jpe", ".jfif":
		return "image/jpeg"
	case ".gif":
		return "image/gif"
	case ".webp":
		return "image/webp"
	case ".bmp":
		return "image/bmp"
	case ".tif", ".tiff":
		return "image/tiff"
	case ".avif":
		return "image/avif"
	case ".svg":
		return "image/svg+xml"
	}
	return ""
}

// imageStatusError 把上游非 2xx 映射成契约 §6/§5.1.4 的错误。
func imageStatusError(statusCode int, raw string) error {
	message := fmt.Sprintf("生图接口 HTTP 错误 %d：%s", statusCode, truncateRunes(raw, 200))
	switch statusCode {
	case http.StatusUnauthorized, http.StatusForbidden:
		return NewAuthError(message, statusCode)
	default:
		return NewResponseError(message, &statusCode)
	}
}

// imageRequestError 把图像链路（请求与下载）过程中的失败归类成超时或连接异常。
func imageRequestError(ctx context.Context, err error, timeout time.Duration) error {
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return NewTimeoutError(imageTimeoutMessage(timeout))
	}
	return NewConnectionError("生图请求异常：" + err.Error())
}

// imageTimeoutMessage 是生图整请求超时的固定文案（契约 §5.1.2.1）。
func imageTimeoutMessage(timeout time.Duration) string {
	return fmt.Sprintf("生图请求超过 %.1f 秒仍未完成，判定为超时。", timeout.Seconds())
}

// imageTimeout 把 timeout_seconds 换算成整请求总超时；异常值兜底 120 秒。
func imageTimeout(seconds float64) time.Duration {
	if seconds <= 0 {
		seconds = defaultImageTimeoutSeconds
	}
	return time.Duration(seconds * float64(time.Second))
}
