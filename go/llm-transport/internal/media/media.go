// Package media 按契约 §5 处理多模态内联：把本地图片路径换成 data URL，
// 网络地址原样透传，无效地址丢弃该 part。
package media

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// Logger 是 media 包的日志出口，由 main 注入 stderr logger；为 nil 时静默。
var Logger *log.Logger

func logf(format string, args ...any) {
	if Logger != nil {
		Logger.Printf(format, args...)
	}
}

// Inline 把 messages 里 image_url part 的本地路径内联成 data URL。
//
// 返回值 prepared 是新的消息切片（不修改入参），conversions 是发生本地路径
// 转换的 part 数，直接进 transport.local_image_conversions（契约 §5.4）。
func Inline(messages []contract.Message) ([]contract.Message, int) {
	if len(messages) == 0 {
		return messages, 0
	}
	prepared := make([]contract.Message, 0, len(messages))
	conversions := 0
	for _, message := range messages {
		contentRaw, ok := message["content"]
		if !ok || !isJSONArray(contentRaw) {
			// content 是纯文本、null 或缺失：原样透传。
			prepared = append(prepared, cloneMessage(message))
			continue
		}
		var parts []json.RawMessage
		if err := json.Unmarshal(contentRaw, &parts); err != nil {
			prepared = append(prepared, cloneMessage(message))
			continue
		}
		preparedParts := make([]json.RawMessage, 0, len(parts))
		for _, part := range parts {
			rewritten, converted, keep := rewritePart(part)
			if !keep {
				continue
			}
			if converted {
				conversions++
			}
			preparedParts = append(preparedParts, rewritten)
		}
		encodedParts, err := json.Marshal(preparedParts)
		if err != nil {
			prepared = append(prepared, cloneMessage(message))
			continue
		}
		clone := cloneMessage(message)
		clone["content"] = encodedParts
		prepared = append(prepared, clone)
	}
	return prepared, conversions
}

// rewritePart 处理单个 content part；keep=false 表示丢弃该 part。
func rewritePart(part json.RawMessage) (json.RawMessage, bool, bool) {
	if !isJSONObject(part) {
		return part, false, true
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(part, &fields); err != nil {
		return part, false, true
	}
	if stringField(fields["type"]) != "image_url" {
		return part, false, true
	}
	imageURLRaw, ok := fields["image_url"]
	if !ok || !isJSONObject(imageURLRaw) {
		return part, false, true
	}
	var imageURL map[string]json.RawMessage
	if err := json.Unmarshal(imageURLRaw, &imageURL); err != nil {
		return part, false, true
	}
	rawURL := strings.TrimSpace(stringField(imageURL["url"]))
	lowered := strings.ToLower(rawURL)
	if strings.HasPrefix(lowered, "http://") || strings.HasPrefix(lowered, "https://") || strings.HasPrefix(lowered, "data:") {
		// 网络地址与 data URL 原样透传（契约 §5.1）。
		return part, false, true
	}
	localPath, found := resolveLocalFile(rawURL)
	if !found {
		// 既不是本地文件也不是网络地址：丢弃，避免上游 400（契约 §5.3）。
		logf("跳过无效的图片 URL（非本地文件且非网络地址）: %s", rawURL)
		return nil, false, false
	}
	dataURL, err := fileToDataURL(localPath)
	if err != nil {
		// Python 侧这里会抛 OSError；侧车选择丢图保请求，只记日志。
		logf("读取本地图片失败，跳过该图片：%s（%v）", localPath, err)
		return nil, false, false
	}
	encodedURL, err := json.Marshal(dataURL)
	if err != nil {
		return nil, false, false
	}
	imageURL["url"] = encodedURL
	encodedImageURL, err := json.Marshal(imageURL)
	if err != nil {
		return nil, false, false
	}
	fields["image_url"] = encodedImageURL
	rewritten, err := json.Marshal(fields)
	if err != nil {
		return nil, false, false
	}
	return rewritten, true, true
}

// fileToDataURL 读取本地文件并按契约 §5.2 生成 data URL。
func fileToDataURL(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return inlineImageBytes(data, MaxInlineImageBytes, MaxInlineImageEdge), nil
}

// inlineImageBytes 把图片字节编成 data URL；超限时先降采样为 JPEG。
func inlineImageBytes(data []byte, maxBytes int, maxEdge int) string {
	if len(data) > maxBytes {
		// base64 会把体积再放大 4/3，超大原图内联前先降采样，避免请求体超限。
		if shrunk, ok := downscaleImageBytes(data, maxBytes, maxEdge); ok {
			data = shrunk
		}
	}
	encoded := base64.StdEncoding.EncodeToString(data)
	return "data:" + SniffImageMime(data) + ";base64," + encoded
}

// resolveLocalFile 判断一个 URL 是否指向本地文件，返回可用路径。
//
// 支持 file:// （含 Windows 盘符形态 file:///D:/a.png 与 UNC）以及裸路径。
func resolveLocalFile(rawURL string) (string, bool) {
	text := strings.TrimSpace(rawURL)
	if text == "" {
		return "", false
	}
	var candidate string
	if strings.HasPrefix(strings.ToLower(text), "file://") {
		remainder := text[len("file://"):]
		if index := strings.IndexAny(remainder, "/\\"); index >= 0 {
			host := remainder[:index]
			if host == "" || strings.EqualFold(host, "localhost") {
				remainder = remainder[index:]
			} else {
				// UNC 路径：file://server/share/a.png → //server/share/a.png
				remainder = "//" + remainder
			}
		}
		unescaped, err := url.PathUnescape(remainder)
		if err != nil {
			unescaped = remainder
		}
		if len(unescaped) >= 3 && unescaped[0] == '/' && unescaped[2] == ':' {
			// Windows 盘符形态：/D:/a.png → D:/a.png
			unescaped = unescaped[1:]
		}
		candidate = unescaped
	} else {
		candidate = expandHome(text)
	}
	info, err := os.Stat(candidate)
	if err != nil || !info.Mode().IsRegular() {
		return "", false
	}
	return candidate, true
}

// expandHome 展开开头的 ~，与 Python 的 Path.expanduser 对齐。
func expandHome(path string) string {
	if !strings.HasPrefix(path, "~/") && !strings.HasPrefix(path, `~\`) {
		return path
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return path
	}
	return filepath.Join(home, path[2:])
}

// cloneMessage 复制消息，避免改写调用方（或 HTTP 请求体）持有的对象。
func cloneMessage(message contract.Message) contract.Message {
	clone := make(contract.Message, len(message)+1)
	for key, value := range message {
		clone[key] = value
	}
	return clone
}

// isJSONArray 判断原始 JSON 是否为数组。
func isJSONArray(raw json.RawMessage) bool {
	trimmed := bytes.TrimLeft(raw, " \t\r\n")
	return len(trimmed) > 0 && trimmed[0] == '['
}

// isJSONObject 判断原始 JSON 是否为对象。
func isJSONObject(raw json.RawMessage) bool {
	trimmed := bytes.TrimLeft(raw, " \t\r\n")
	return len(trimmed) > 0 && trimmed[0] == '{'
}

// stringField 读取一个 JSON 字符串字段；非字符串或缺失返回空串。
func stringField(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var text string
	if err := json.Unmarshal(raw, &text); err != nil {
		return ""
	}
	return text
}
