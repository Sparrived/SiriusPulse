package upstream

import (
	"fmt"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// Error 描述一次上游调用的失败，携带契约 §6 的错误码、返回状态与重试建议。
//
// 它是 sidecar 内部唯一的失败类型：server 层只做「Error → HTTP 响应」的搬运，
// 不再二次判断语义，避免两处规则漂移。
type Error struct {
	// Code 是契约 §6 的错误码。
	Code string
	// HTTPStatus 是返回给 Python 的 HTTP 状态。
	HTTPStatus int
	// Message 是面向排障的中文文案，与 Python 现实现的措辞保持一致。
	Message string
	// UpstreamStatus 是上游状态码，无则为 nil（契约 §6 的 null）。
	UpstreamStatus *int
	// Retryable 对应 Python 异常的 is_retryable。
	Retryable bool
	// ContentPreview 仅在 PROVIDER_CONTENT_FILTERED 时填充。
	ContentPreview string
}

func (e *Error) Error() string { return e.Message }

// ResponseErrorRetryable 实现契约 §6 的重试判定，与
// sirius_pulse.exceptions._http_status_is_retryable 保持一致：
//
//   - 429（限流）与 5xx（服务端/网关瞬时故障）值得重试；
//   - 其余 4xx 是请求本身的问题，重试不会变好；
//   - 没有状态码（本地判定）按可重试处理，保持旧行为。
func ResponseErrorRetryable(httpStatus *int) bool {
	if httpStatus == nil {
		return true
	}
	return *httpStatus == 429 || *httpStatus >= 500
}

// NewAuthError 构造 PROVIDER_AUTH_ERROR（上游 401/403）。
func NewAuthError(message string, upstreamStatus int) *Error {
	return &Error{
		Code:           contract.CodeProviderAuthError,
		HTTPStatus:     contract.StatusProviderFailure,
		Message:        message,
		UpstreamStatus: &upstreamStatus,
		Retryable:      false,
	}
}

// NewResponseError 构造 PROVIDER_RESPONSE_ERROR；upstreamStatus 为 nil 表示本地判定。
func NewResponseError(message string, upstreamStatus *int) *Error {
	return &Error{
		Code:           contract.CodeProviderResponseError,
		HTTPStatus:     contract.StatusProviderFailure,
		Message:        message,
		UpstreamStatus: upstreamStatus,
		Retryable:      ResponseErrorRetryable(upstreamStatus),
	}
}

// NewContentFilteredError 构造 PROVIDER_CONTENT_FILTERED（契约 §4.7）。
func NewContentFilteredError(message string, contentPreview string) *Error {
	return &Error{
		Code:           contract.CodeProviderContentFiltered,
		HTTPStatus:     contract.StatusProviderFailure,
		Message:        message,
		Retryable:      false,
		ContentPreview: contentPreview,
	}
}

// NewTimeoutError 构造 UPSTREAM_TIMEOUT（契约 §4.2 的块间静默超时）。
func NewTimeoutError(message string) *Error {
	return &Error{
		Code:       contract.CodeUpstreamTimeout,
		HTTPStatus: contract.StatusUpstreamTimeout,
		Message:    message,
		Retryable:  true,
	}
}

// NewConnectionError 构造 PROVIDER_CONNECTION_ERROR（连不上、DNS/TLS 失败、连接中断）。
func NewConnectionError(message string) *Error {
	return &Error{
		Code:       contract.CodeProviderConnectionError,
		HTTPStatus: contract.StatusProviderFailure,
		Message:    message,
		Retryable:  true,
	}
}

// NewInvalidRequestError 构造 INVALID_REQUEST（sidecar 自身的入参校验失败）。
func NewInvalidRequestError(message string) *Error {
	return &Error{
		Code:       contract.CodeInvalidRequest,
		HTTPStatus: contract.StatusInvalidRequest,
		Message:    message,
		Retryable:  false,
	}
}

// NewUnauthorizedError 构造 UNAUTHORIZED（sidecar 自身 token 不匹配）。
func NewUnauthorizedError(message string) *Error {
	return &Error{
		Code:       contract.CodeUnauthorized,
		HTTPStatus: contract.StatusUnauthorized,
		Message:    message,
		Retryable:  false,
	}
}

// statusError 把上游的非 2xx 响应映射成契约 §6 的错误。
func statusError(statusCode int, raw string) *Error {
	message := fmt.Sprintf("提供商 HTTP 错误 %d：%s", statusCode, truncateRunes(raw, 200))
	if statusCode == 400 && containsSubstring(raw, "Failed to download multimodal content") {
		// 沿用 Python 的排障提示：这类 400 基本都是图片地址上游下载不到。
		message += "。多模态文件下载失败：请确认 image_url 使用公网可访问的 http/https URL，" +
			"且响应头包含 Content-Type 与 Content-Length；若传入的是本地图片路径，" +
			"请直接传本地文件路径让框架自动转换为 data URL，或自行传入 data:*;base64,...。"
	}
	switch statusCode {
	case 401, 403:
		return NewAuthError(message, statusCode)
	default:
		return NewResponseError(message, &statusCode)
	}
}
