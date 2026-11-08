// Package server 提供 sidecar 的三个端点：POST /v1/generate、POST /v1/generate-image
// 与 GET /health。
package server

import (
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/media"
	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/upstream"
)

// Server 是 sidecar 的 HTTP 服务。
type Server struct {
	// Token 是 Python 侧传入的共享 token，所有 /v1/* 请求必须带 Authorization: Bearer。
	Token string
	// Version 是 /health 上报的语义化版本。
	Version string
	// PID 是 /health 上报的进程号。
	PID int
	// Client 是上游客户端，测试可替换。
	Client *upstream.Client
	// Logger 是日志出口（stderr），可为 nil。
	Logger *log.Logger
}

// New 构造服务；logger 同时注入 media 包用于「丢弃图片」的诊断日志。
func New(token string, version string, logger *log.Logger) *Server {
	media.Logger = logger
	return &Server{
		Token:   token,
		Version: version,
		PID:     os.Getpid(),
		Client:  upstream.NewClient(),
		Logger:  logger,
	}
}

// Handler 返回挂好路由的 http.Handler。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("POST /v1/generate", s.authorize(s.handleGenerate))
	mux.HandleFunc("POST /v1/generate-image", s.authorize(s.handleGenerateImage))
	return mux
}

// handleHealth 返回就绪信息，免鉴权（契约 §1）。
func (s *Server) handleHealth(writer http.ResponseWriter, _ *http.Request) {
	s.writeJSON(writer, http.StatusOK, contract.HealthResponse{
		Status:  "ok",
		Version: s.Version,
		PID:     s.PID,
	})
}

// authorize 校验 Bearer token（契约 §1/§6）。
func (s *Server) authorize(next http.HandlerFunc) http.HandlerFunc {
	return func(writer http.ResponseWriter, request *http.Request) {
		presented, ok := bearerToken(request.Header.Get("Authorization"))
		if !ok || subtle.ConstantTimeCompare([]byte(presented), []byte(s.Token)) != 1 {
			s.writeError(writer, upstream.NewUnauthorizedError("sidecar 鉴权失败：Authorization 头的 Bearer token 不匹配。"))
			return
		}
		next(writer, request)
	}
}

// handleGenerate 是 POST /v1/generate 的入口（契约 §2/§3/§6）。
func (s *Server) handleGenerate(writer http.ResponseWriter, request *http.Request) {
	started := time.Now()
	var payload contract.GenerateRequest
	decoder := json.NewDecoder(request.Body)
	if err := decoder.Decode(&payload); err != nil {
		s.reject(writer, "请求体不是合法 JSON："+err.Error(), started)
		return
	}
	if validationErr := validateUpstream(payload.Upstream); validationErr != "" {
		s.reject(writer, validationErr, started)
		return
	}

	preparedMessages, conversions := media.Inline(payload.Request.Messages)
	payload.Request.Messages = preparedMessages

	result, err := s.Client.Generate(request.Context(), &payload)
	if err != nil {
		upstreamErr := asUpstreamError(err)
		s.writeError(writer, upstreamErr)
		s.logf("调用失败 | model=%s | purpose=%s | status=%d | error=%s | %s | %.1fms",
			payload.Request.Model, payload.Request.Purpose, upstreamErr.HTTPStatus,
			upstreamErr.Code, upstreamErr.Message, elapsedMilliseconds(started))
		return
	}

	s.writeJSON(writer, http.StatusOK, contract.GenerateResponse{
		Content:          result.Content,
		ReasoningContent: result.ReasoningContent,
		ToolCalls:        result.ToolCalls,
		FinishReason:     result.FinishReason,
		Usage:            result.Usage,
		Transport: contract.Transport{
			ContentType:           result.ContentType,
			LocalImageConversions: conversions,
			UpstreamMs:            result.UpstreamMs,
			Path:                  result.Path,
		},
	})
	s.logf("调用成功 | model=%s | purpose=%s | path=%s | 内容 %d 字 | 工具调用 %d | 图片内联 %d | %.1fms",
		payload.Request.Model, payload.Request.Purpose, result.Path,
		len([]rune(result.Content)), len(result.ToolCalls), conversions, result.UpstreamMs)
}

// handleGenerateImage 是 POST /v1/generate-image 的入口（契约 §5.1/§6）。
func (s *Server) handleGenerateImage(writer http.ResponseWriter, request *http.Request) {
	started := time.Now()
	var payload contract.GenerateImageRequest
	decoder := json.NewDecoder(request.Body)
	if err := decoder.Decode(&payload); err != nil {
		s.reject(writer, "请求体不是合法 JSON："+err.Error(), started)
		return
	}
	if validationErr := validateUpstream(payload.Upstream); validationErr != "" {
		s.reject(writer, validationErr, started)
		return
	}

	result, err := s.Client.GenerateImage(request.Context(), &payload)
	if err != nil {
		upstreamErr := asUpstreamError(err)
		s.writeError(writer, upstreamErr)
		s.logf("生图失败 | model=%s | 参考图 %d 张 | status=%d | error=%s | %s | %.1fms",
			payload.Request.Model, len(payload.Request.ReferenceImages), upstreamErr.HTTPStatus,
			upstreamErr.Code, upstreamErr.Message, elapsedMilliseconds(started))
		return
	}

	s.writeJSON(writer, http.StatusOK, contract.GenerateImageResponse{
		ImageBase64: base64.StdEncoding.EncodeToString(result.ImageBytes),
		MimeType:    result.MimeType,
		Transport: contract.ImageTransport{
			Endpoint:   result.Endpoint,
			Downloaded: result.Downloaded,
			UpstreamMs: result.UpstreamMs,
		},
	})
	s.logf("生图成功 | model=%s | endpoint=%s | %s | %d 字节 | 下载=%t | %.1fms",
		payload.Request.Model, result.Endpoint, result.MimeType, len(result.ImageBytes),
		result.Downloaded, result.UpstreamMs)
}

// reject 输出 INVALID_REQUEST（契约 §6 的 400 行）。
func (s *Server) reject(writer http.ResponseWriter, message string, started time.Time) {
	upstreamErr := upstream.NewInvalidRequestError(message)
	s.writeError(writer, upstreamErr)
	s.logf("拒绝请求 | status=%d | %s | %.1fms", upstreamErr.HTTPStatus, message, elapsedMilliseconds(started))
}

// writeError 把错误按契约 §6 的响应体写回。
func (s *Server) writeError(writer http.ResponseWriter, err *upstream.Error) {
	status := err.HTTPStatus
	if status == 0 {
		status = contract.StatusProviderFailure
	}
	s.writeJSON(writer, status, contract.ErrorResponse{Error: contract.ErrorBody{
		ErrorCode:      err.Code,
		Message:        err.Message,
		HTTPStatus:     status,
		UpstreamStatus: err.UpstreamStatus,
		IsRetryable:    err.Retryable,
		ContentPreview: err.ContentPreview,
	}})
}

// writeJSON 输出 JSON 响应。
func (s *Server) writeJSON(writer http.ResponseWriter, status int, body any) {
	encoded, err := json.Marshal(body)
	if err != nil {
		http.Error(writer, "编码响应失败", http.StatusInternalServerError)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_, _ = writer.Write(encoded)
}

func (s *Server) logf(format string, args ...any) {
	if s.Logger != nil {
		s.Logger.Printf(format, args...)
	}
}

// validateUpstream 校验两类端点共用的 400 条件（契约 §6）。
func validateUpstream(config contract.Upstream) string {
	if strings.TrimSpace(config.BaseURL) == "" {
		return "缺少 upstream.base_url。"
	}
	if strings.TrimSpace(config.APIKey) == "" {
		return "缺少 upstream.api_key。"
	}
	if config.TimeoutSeconds <= 0 {
		return "upstream.timeout_seconds 必须大于 0。"
	}
	return ""
}

// asUpstreamError 把任意失败收敛成 *upstream.Error。
//
// 上游失败都由 upstream 包构造好；这里的兜底只覆盖上下文取消之类的框架错误。
func asUpstreamError(err error) *upstream.Error {
	var upstreamErr *upstream.Error
	if errors.As(err, &upstreamErr) {
		return upstreamErr
	}
	return upstream.NewConnectionError("提供商请求异常：" + err.Error())
}

// bearerToken 从 Authorization 头取出 Bearer token。
func bearerToken(header string) (string, bool) {
	const scheme = "bearer "
	trimmed := strings.TrimSpace(header)
	if len(trimmed) <= len(scheme) || !strings.EqualFold(trimmed[:len(scheme)], scheme) {
		return "", false
	}
	token := strings.TrimSpace(trimmed[len(scheme):])
	if token == "" {
		return "", false
	}
	return token, true
}

// elapsedMilliseconds 把耗时换算成毫秒。
func elapsedMilliseconds(started time.Time) float64 {
	return float64(time.Since(started).Microseconds()) / 1000.0
}
