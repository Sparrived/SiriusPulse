package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// chatCompletionsPath 是 AMKR 的生成端点（契约 §4 恒定流式）。
const chatCompletionsPath = "/v1/chat/completions"

// maxErrorBodyBytes 限制读取错误响应体的上限，避免异常上游把 sidecar 内存打满。
const maxErrorBodyBytes = 1 << 20

// Client 是 AMKR chat/completions 的 HTTP 客户端。
//
// HTTPClient 不设 Timeout：契约 §4.2 要求的是「块间静默超时」而不是整请求总时长，
// 总时长限制会砍掉长生成。静默超时由 watchdog 逐次读重置实现。
type Client struct {
	HTTPClient *http.Client
}

// NewClient 构造默认客户端。
func NewClient() *Client {
	return &Client{HTTPClient: &http.Client{Transport: http.DefaultTransport}}
}

// Generate 按契约 §2/§4 调用 AMKR 并把流式增量聚合成完整结果。
//
// 返回的 *Error 已经带好错误码、返回状态与重试建议，调用方只需搬运。
func (c *Client) Generate(ctx context.Context, req *contract.GenerateRequest) (*Result, error) {
	payload, err := buildRequestPayload(&req.Request)
	if err != nil {
		return nil, NewResponseError("构造上游请求体失败："+err.Error(), nil)
	}
	url := strings.TrimRight(strings.TrimSpace(req.Upstream.BaseURL), "/") + chatCompletionsPath
	timeout := time.Duration(req.Upstream.TimeoutSeconds * float64(time.Second))

	requestCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	httpRequest, err := http.NewRequestWithContext(requestCtx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return nil, NewConnectionError("构造上游请求失败：" + err.Error())
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	httpRequest.Header.Set("Authorization", "Bearer "+req.Upstream.APIKey)
	if workspace := strings.TrimSpace(req.Upstream.Workspace); workspace != "" {
		httpRequest.Header.Set("X-AMKR-Workspace", workspace)
	}

	watchdog := newWatchdog(timeout, cancel)
	defer watchdog.stop()
	started := time.Now()

	response, err := c.HTTPClient.Do(httpRequest)
	if err != nil {
		if watchdog.fired() {
			return nil, NewTimeoutError(silenceTimeoutMessage(timeout))
		}
		return nil, NewConnectionError("提供商请求异常：" + err.Error())
	}
	defer func() {
		_ = response.Body.Close()
	}()

	contentType := response.Header.Get("Content-Type")
	body := &idleBody{ReadCloser: response.Body, watchdog: watchdog}

	if response.StatusCode >= 400 {
		raw, _ := io.ReadAll(io.LimitReader(body, maxErrorBodyBytes))
		return nil, statusError(response.StatusCode, string(raw))
	}

	var result *Result
	if isEventStream(contentType) {
		result, err = aggregateEventStream(body)
		if err != nil {
			return nil, classifyStreamError(err, watchdog, timeout)
		}
		result.Path = contract.PathSSE
	} else {
		raw, readErr := io.ReadAll(body)
		if readErr != nil {
			return nil, classifyStreamError(readErr, watchdog, timeout)
		}
		result, err = parseCompletionBody(raw, response.StatusCode)
		if err != nil {
			return nil, err
		}
		result.Path = contract.PathJSON
	}
	result.ContentType = contentType
	result.UpstreamMs = elapsedMilliseconds(started)

	// 空响应（content 为空且没有 tool_calls）不在这里判失败：契约 §4.6 把它留给
	// Python —— return_reasoning=True 只取思维链是合法场景，是否算失败属于业务语义。
	return result, nil
}

// buildRequestPayload 组装发往 AMKR 的请求体（契约 §2）。
//
// 不写任何采样参数：AMKR 的任务定义才是权威，多传会 400。
func buildRequestPayload(request *contract.CompletionRequest) ([]byte, error) {
	messages := make([]json.RawMessage, 0, len(request.Messages)+1)
	systemMessage, err := json.Marshal(map[string]string{
		"role":    "system",
		"content": request.SystemPrompt,
	})
	if err != nil {
		return nil, err
	}
	messages = append(messages, systemMessage)
	for _, message := range request.Messages {
		encoded, marshalErr := json.Marshal(message)
		if marshalErr != nil {
			return nil, marshalErr
		}
		messages = append(messages, encoded)
	}
	messagesJSON, err := json.Marshal(messages)
	if err != nil {
		return nil, err
	}
	modelJSON, err := json.Marshal(request.Model)
	if err != nil {
		return nil, err
	}
	payload := map[string]json.RawMessage{
		"model":          modelJSON,
		"messages":       messagesJSON,
		"stream":         json.RawMessage("true"),
		"stream_options": json.RawMessage(`{"include_usage":true}`),
	}
	if !isNullJSON(request.Tools) {
		payload["tools"] = request.Tools
	}
	if !isNullJSON(request.ToolChoice) {
		payload["tool_choice"] = request.ToolChoice
	}
	if !isNullJSON(request.ResponseFormat) {
		payload["response_format"] = request.ResponseFormat
	}
	return json.Marshal(payload)
}

// isEventStream 判断响应是否为 SSE（契约 §4.9）。
func isEventStream(contentType string) bool {
	return strings.Contains(strings.ToLower(contentType), "text/event-stream")
}

// classifyStreamError 把读流过程中的失败归类成超时或连接中断。
func classifyStreamError(err error, watchdog *watchdog, timeout time.Duration) error {
	var upstreamErr *Error
	if errors.As(err, &upstreamErr) {
		return upstreamErr
	}
	if watchdog.fired() {
		return NewTimeoutError(silenceTimeoutMessage(timeout))
	}
	return NewConnectionError("提供商请求异常：" + err.Error())
}

// silenceTimeoutMessage 是块间静默超时的固定文案。
func silenceTimeoutMessage(timeout time.Duration) string {
	return fmt.Sprintf("上游连续 %s 没有返回数据，判定为超时。", timeout)
}

// elapsedMilliseconds 把耗时换算成毫秒（保留小数，契约 §3 的 upstream_ms）。
func elapsedMilliseconds(started time.Time) float64 {
	return float64(time.Since(started).Microseconds()) / 1000.0
}

// watchdog 是块间静默超时的看门狗：连续 timeout 没有读到数据就取消请求。
type watchdog struct {
	timeout time.Duration
	timer   *time.Timer
	hit     atomic.Bool
}

func newWatchdog(timeout time.Duration, cancel context.CancelFunc) *watchdog {
	guard := &watchdog{timeout: timeout}
	guard.timer = time.AfterFunc(timeout, func() {
		guard.hit.Store(true)
		cancel()
	})
	return guard
}

// reset 在每次读到数据后重新计时。
func (w *watchdog) reset() {
	if w == nil {
		return
	}
	w.timer.Reset(w.timeout)
}

// stop 结束看门狗。
func (w *watchdog) stop() {
	if w == nil {
		return
	}
	w.timer.Stop()
}

// fired 报告是否已经触发过静默超时。
func (w *watchdog) fired() bool {
	if w == nil {
		return false
	}
	return w.hit.Load()
}

// idleBody 在每次读到数据时重置看门狗。
type idleBody struct {
	io.ReadCloser
	watchdog *watchdog
}

func (b *idleBody) Read(buffer []byte) (int, error) {
	read, err := b.ReadCloser.Read(buffer)
	if read > 0 {
		b.watchdog.reset()
	}
	return read, err
}
