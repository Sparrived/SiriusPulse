// Package contract 定义 LLM transport sidecar 与 Python 侧共用的线协议。
//
// 字段名与语义以 go/llm-transport/CONTRACT.md（v1.2）为唯一权威；本包只做
// JSON 结构与常量声明，不承载任何业务逻辑。
package contract

import "encoding/json"

// Version 是 sidecar 的语义化版本，供 GET /health 与 Python 侧主版本校验使用（契约 §8/§9）。
const Version = "0.2.0"

// 错误码（契约 §6）。
const (
	CodeInvalidRequest          = "INVALID_REQUEST"
	CodeUnauthorized            = "UNAUTHORIZED"
	CodeProviderAuthError       = "PROVIDER_AUTH_ERROR"
	CodeProviderResponseError   = "PROVIDER_RESPONSE_ERROR"
	CodeProviderContentFiltered = "PROVIDER_CONTENT_FILTERED"
	CodeUpstreamTimeout         = "UPSTREAM_TIMEOUT"
	CodeProviderConnectionError = "PROVIDER_CONNECTION_ERROR"
)

// 传输路径标记（契约 §3 transport.path）。
const (
	PathSSE  = "sse"
	PathJSON = "json"
)

// 图像端点标记（契约 §5.1.3 transport.endpoint）。
const (
	// EndpointGenerations 表示没有参考图，走 /v1/images/generations。
	EndpointGenerations = "generations"
	// EndpointEdits 表示有参考图，走 multipart 的 /v1/images/edits。
	EndpointEdits = "edits"
	// EndpointEditsJSON 表示 multipart 被 4xx 拒绝后回退成功的 JSON edits。
	EndpointEditsJSON = "edits-json"
)

// 返回给 Python 的 HTTP 状态码（契约 §6）。
const (
	StatusInvalidRequest  = 400
	StatusUnauthorized    = 401
	StatusProviderFailure = 502
	StatusUpstreamTimeout = 504
)

// GenerateRequest 是 POST /v1/generate 的请求体（契约 §2）。
type GenerateRequest struct {
	Upstream Upstream          `json:"upstream"`
	Request  CompletionRequest `json:"request"`
}

// Upstream 描述这次调用指向的 AMKR 工作空间；凭据逐请求传入，sidecar 不落盘。
type Upstream struct {
	BaseURL string `json:"base_url"`
	APIKey  string `json:"api_key"`
	// Workspace 可空；非空时作为 X-AMKR-Workspace 发送。
	Workspace string `json:"workspace"`
	// TimeoutSeconds 是超时秒数，必须大于 0：聊天链路是块间静默超时（契约 §4.2），
	// 生图端点是整请求总超时（契约 §5.1.2）。
	TimeoutSeconds float64 `json:"timeout_seconds"`
}

// CompletionRequest 是发往 AMKR 的生成请求（不含任何采样参数，见契约 §2）。
type CompletionRequest struct {
	Model        string    `json:"model"`
	SystemPrompt string    `json:"system_prompt"`
	Messages     []Message `json:"messages"`
	// Tools / ToolChoice / ResponseFormat 原样透传；JSON null 表示不发。
	Tools          json.RawMessage `json:"tools"`
	ToolChoice     json.RawMessage `json:"tool_choice"`
	ResponseFormat json.RawMessage `json:"response_format"`
	// Purpose 仅用于日志。
	Purpose string `json:"purpose"`
}

// Message 是一条 OpenAI 兼容消息。
//
// 用 map 而非定长结构体是为了让 tool_calls / tool_call_id / name 等字段
// 无损透传：契约允许 Python 侧把历史消息原样交给 sidecar。
type Message map[string]json.RawMessage

// GenerateResponse 是成功响应体（契约 §3）。
type GenerateResponse struct {
	Content          string     `json:"content"`
	ReasoningContent string     `json:"reasoning_content"`
	ToolCalls        []ToolCall `json:"tool_calls"`
	FinishReason     string     `json:"finish_reason"`
	// Usage 是上游原始 usage 对象，无则 null，不得改写。
	Usage     json.RawMessage `json:"usage"`
	Transport Transport       `json:"transport"`
}

// ToolCall 是聚合后的工具调用（契约 §3）。
type ToolCall struct {
	ID                string `json:"id"`
	Type              string `json:"type"`
	FunctionName      string `json:"function_name"`
	FunctionArguments string `json:"function_arguments"`
}

// Transport 是本次调用的传输层统计（契约 §3）。
type Transport struct {
	ContentType           string  `json:"content_type"`
	LocalImageConversions int     `json:"local_image_conversions"`
	UpstreamMs            float64 `json:"upstream_ms"`
	Path                  string  `json:"path"`
}

// GenerateImageRequest 是 POST /v1/generate-image 的请求体（契约 §5.1.1）。
type GenerateImageRequest struct {
	Upstream Upstream               `json:"upstream"`
	Request  ImageGenerationRequest `json:"request"`
}

// ImageGenerationRequest 是发往 AMKR 的生图/图像编辑请求（契约 §5.1.1）。
type ImageGenerationRequest struct {
	// Model 是 AMKR 任务名，原样透传。
	Model  string `json:"model"`
	Prompt string `json:"prompt"`
	// ReferenceImages 可缺省；为空走 generations，非空走 edits。
	ReferenceImages []ImageReference `json:"reference_images"`
}

// ImageReference 是一张参考图（契约 §5.1.1）。
//
// DataBase64 是参考图**原始字节**的 base64：参考图来自内存里的表情库或用户上传，
// 不保证有可读的本地路径，因此不能用路径传递；Filename 只用于兜底推断 mime。
type ImageReference struct {
	Filename   string `json:"filename"`
	DataBase64 string `json:"data_base64"`
}

// GenerateImageResponse 是 POST /v1/generate-image 的成功响应体（契约 §5.1.3）。
type GenerateImageResponse struct {
	// ImageBase64 是生成图片原始字节的 base64。
	ImageBase64 string         `json:"image_base64"`
	MimeType    string         `json:"mime_type"`
	Transport   ImageTransport `json:"transport"`
}

// ImageTransport 是本次生图的传输层统计（契约 §5.1.3）。
type ImageTransport struct {
	// Endpoint 取 EndpointGenerations / EndpointEdits / EndpointEditsJSON。
	Endpoint string `json:"endpoint"`
	// Downloaded 表示这次是否走了 url 下载。
	Downloaded bool `json:"downloaded"`
	// UpstreamMs 是整次生图（含下载）的耗时（毫秒）。
	UpstreamMs float64 `json:"upstream_ms"`
}

// HealthResponse 是 GET /health 的响应体（契约 §1）。
type HealthResponse struct {
	Status  string `json:"status"`
	Version string `json:"version"`
	PID     int    `json:"pid"`
}

// ErrorResponse 是失败响应体（契约 §6）。
type ErrorResponse struct {
	Error ErrorBody `json:"error"`
}

// ErrorBody 是失败响应的错误详情（契约 §6）。
type ErrorBody struct {
	ErrorCode string `json:"error_code"`
	Message   string `json:"message"`
	// HTTPStatus 是 sidecar 返回给 Python 的状态码。
	HTTPStatus int `json:"http_status"`
	// UpstreamStatus 是上游状态码，可为 null。
	UpstreamStatus *int `json:"upstream_status"`
	IsRetryable    bool `json:"is_retryable"`
	// ContentPreview 仅在 PROVIDER_CONTENT_FILTERED 时给出。
	ContentPreview string `json:"content_preview,omitempty"`
}
