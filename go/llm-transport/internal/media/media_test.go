package media

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// noiseImage 生成噪点图：压缩率很低，便于把小阈值下的降采样路径压出来。
func noiseImage(width int, height int) image.Image {
	canvas := image.NewRGBA(image.Rect(0, 0, width, height))
	state := uint32(2166136261)
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			state = state*1664525 + 1013904223
			canvas.SetRGBA(x, y, color.RGBA{R: uint8(state >> 16), G: uint8(state >> 8), B: uint8(state), A: 0xff})
		}
	}
	return canvas
}

// encodePNG 把图片编成 PNG 字节。
func encodePNG(t *testing.T, source image.Image) []byte {
	t.Helper()
	var buffer bytes.Buffer
	if err := png.Encode(&buffer, source); err != nil {
		t.Fatalf("编码 PNG 失败: %v", err)
	}
	return buffer.Bytes()
}

// writePNGFile 在临时目录写一张 PNG。
func writePNGFile(t *testing.T, dir string, name string, width int, height int) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, encodePNG(t, noiseImage(width, height)), 0o600); err != nil {
		t.Fatalf("写入图片失败: %v", err)
	}
	return path
}

// imagePartsMessage 构造一条只含图片/文本 part 的用户消息。
func imagePartsMessage(t *testing.T, parts ...map[string]any) contract.Message {
	t.Helper()
	encoded, err := json.Marshal(parts)
	if err != nil {
		t.Fatalf("构造 parts 失败: %v", err)
	}
	return contract.Message{
		"role":    json.RawMessage(`"user"`),
		"content": encoded,
	}
}

// imageURLPart 构造一个 image_url part。
func imageURLPart(url string) map[string]any {
	return map[string]any{"type": "image_url", "image_url": map[string]any{"url": url}}
}

// contentParts 取出消息的 content 数组。
func contentParts(t *testing.T, message contract.Message) []map[string]json.RawMessage {
	t.Helper()
	var parts []map[string]json.RawMessage
	if err := json.Unmarshal(message["content"], &parts); err != nil {
		t.Fatalf("content 不是 part 数组: %v", err)
	}
	return parts
}

// partURL 取出 part 的 image_url.url。
func partURL(t *testing.T, part map[string]json.RawMessage) string {
	t.Helper()
	var imageURL map[string]json.RawMessage
	if err := json.Unmarshal(part["image_url"], &imageURL); err != nil {
		t.Fatalf("image_url 结构异常: %v", err)
	}
	var url string
	if err := json.Unmarshal(imageURL["url"], &url); err != nil {
		t.Fatalf("url 不是字符串: %v", err)
	}
	return url
}

// splitDataURL 解析 data URL，返回媒体类型与字节。
func splitDataURL(t *testing.T, dataURL string) (string, []byte) {
	t.Helper()
	const prefix = "data:"
	if !strings.HasPrefix(dataURL, prefix) {
		t.Fatalf("不是 data URL: %.60s", dataURL)
	}
	remainder := dataURL[len(prefix):]
	comma := strings.Index(remainder, ",")
	if comma < 0 {
		t.Fatalf("data URL 缺少分隔符: %.60s", dataURL)
	}
	metadata := remainder[:comma]
	payload := remainder[comma+1:]
	if !strings.HasSuffix(metadata, ";base64") {
		t.Fatalf("data URL 不是 base64 编码: %s", metadata)
	}
	decoded, err := base64.StdEncoding.DecodeString(payload)
	if err != nil {
		t.Fatalf("base64 解码失败: %v", err)
	}
	return strings.TrimSuffix(metadata, ";base64"), decoded
}

// TestSniffImageMime 覆盖契约 §5.2 的魔数判定与兜底。
func TestSniffImageMime(t *testing.T) {
	cases := []struct {
		name string
		data []byte
		want string
	}{
		{"png", []byte{0x89, 'P', 'N', 'G', 0x0d, 0x0a, 0x1a, 0x0a, 0x00}, "image/png"},
		{"jpeg", []byte{0xff, 0xd8, 0xff, 0xe0, 0x00}, "image/jpeg"},
		{"gif87", []byte("GIF87a...."), "image/gif"},
		{"gif89", []byte("GIF89a...."), "image/gif"},
		{"webp", []byte("RIFF\x24\x00\x00\x00WEBPVP8 "), "image/webp"},
		{"认不出时兜底", []byte("这不是图片"), "image/jpeg"},
		{"空字节", nil, "image/jpeg"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := SniffImageMime(testCase.data); got != testCase.want {
				t.Fatalf("SniffImageMime = %q, want %q", got, testCase.want)
			}
		})
	}
}

// TestInlineConstantsMatchContract 锁定契约 §5.2 的阈值。
func TestInlineConstantsMatchContract(t *testing.T) {
	if MaxInlineImageBytes != 10*1024*1024 {
		t.Fatalf("MaxInlineImageBytes = %d", MaxInlineImageBytes)
	}
	if MaxInlineImageEdge != 1568 {
		t.Fatalf("MaxInlineImageEdge = %d", MaxInlineImageEdge)
	}
}

// TestInlineConvertsLocalImagePath 覆盖本地路径 → data URL 与计数。
func TestInlineConvertsLocalImagePath(t *testing.T) {
	dir := t.TempDir()
	path := writePNGFile(t, dir, "sample.png", 24, 16)
	originalBytes, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取图片失败: %v", err)
	}
	message := imagePartsMessage(t, imageURLPart(path), map[string]any{"type": "text", "text": "看图"})

	prepared, conversions := Inline([]contract.Message{message})
	if conversions != 1 {
		t.Fatalf("conversions = %d", conversions)
	}
	if len(prepared) != 1 {
		t.Fatalf("messages 数量 = %d", len(prepared))
	}
	parts := contentParts(t, prepared[0])
	if len(parts) != 2 {
		t.Fatalf("part 数量 = %d", len(parts))
	}
	mime, data := splitDataURL(t, partURL(t, parts[0]))
	if mime != "image/png" {
		t.Fatalf("mime = %q", mime)
	}
	if !bytes.Equal(data, originalBytes) {
		t.Fatal("内联字节与源文件不一致")
	}
	var partType, partText string
	if err := json.Unmarshal(parts[1]["type"], &partType); err != nil {
		t.Fatalf("文本 part 的 type 结构异常: %v", err)
	}
	if err := json.Unmarshal(parts[1]["text"], &partText); err != nil {
		t.Fatalf("文本 part 的 text 结构异常: %v", err)
	}
	if partType != "text" || partText != "看图" {
		t.Fatalf("文本 part = %s/%s", partType, partText)
	}
	// 入参不能被就地改写。
	if parts := contentParts(t, message); partURL(t, parts[0]) != path {
		t.Fatalf("入参被改写: %s", partURL(t, parts[0]))
	}
}

// TestInlineAcceptsFileSchemeWithEscapedPath 覆盖 file:/// 形态与 %20 转义。
func TestInlineAcceptsFileSchemeWithEscapedPath(t *testing.T) {
	dir := t.TempDir()
	path := writePNGFile(t, dir, "a b.png", 8, 8)
	fileURL := "file:///" + strings.ReplaceAll(filepath.ToSlash(path), " ", "%20")

	prepared, conversions := Inline([]contract.Message{imagePartsMessage(t, imageURLPart(fileURL))})
	if conversions != 1 {
		t.Fatalf("conversions = %d", conversions)
	}
	mime, _ := splitDataURL(t, partURL(t, contentParts(t, prepared[0])[0]))
	if mime != "image/png" {
		t.Fatalf("mime = %q", mime)
	}
}

// TestInlineKeepsNetworkImageURLs 覆盖网络地址与 data URL 原样透传（契约 §5.1）。
func TestInlineKeepsNetworkImageURLs(t *testing.T) {
	urls := []string{
		"https://example.com/a.png",
		"HTTP://example.com/b.jpg",
		"data:image/png;base64,iVBORw0KGgo=",
	}
	for _, url := range urls {
		prepared, conversions := Inline([]contract.Message{imagePartsMessage(t, imageURLPart(url))})
		if conversions != 0 {
			t.Fatalf("%s 不应计入转换次数", url)
		}
		if got := partURL(t, contentParts(t, prepared[0])[0]); got != url {
			t.Fatalf("url 被改写: %q", got)
		}
	}
}

// TestInlineDropsInvalidImageURL 覆盖无效地址丢弃该 part（契约 §5.3）。
func TestInlineDropsInvalidImageURL(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "gone.png")
	message := imagePartsMessage(t, imageURLPart(missing), map[string]any{"type": "text", "text": "还在"})

	prepared, conversions := Inline([]contract.Message{message})
	if conversions != 0 {
		t.Fatalf("conversions = %d", conversions)
	}
	parts := contentParts(t, prepared[0])
	if len(parts) != 1 {
		t.Fatalf("part 数量 = %d", len(parts))
	}
	var remainingText string
	if err := json.Unmarshal(parts[0]["text"], &remainingText); err != nil {
		t.Fatalf("剩余 part 结构异常: %v", err)
	}
	if remainingText != "还在" {
		t.Fatalf("剩余 part 文本 = %q", remainingText)
	}
}

// TestInlinePassesThroughNonArrayContent 覆盖纯文本与 null content。
func TestInlinePassesThroughNonArrayContent(t *testing.T) {
	cases := []contract.Message{
		{"role": json.RawMessage(`"user"`), "content": json.RawMessage(`"纯文本"`)},
		{"role": json.RawMessage(`"assistant"`), "content": json.RawMessage(`null`)},
		{"role": json.RawMessage(`"tool"`), "tool_call_id": json.RawMessage(`"call_1"`)},
	}
	for _, message := range cases {
		prepared, conversions := Inline([]contract.Message{message})
		if conversions != 0 {
			t.Fatalf("conversions = %d", conversions)
		}
		if len(prepared) != 1 {
			t.Fatalf("messages 数量 = %d", len(prepared))
		}
		if string(prepared[0]["content"]) != string(message["content"]) {
			t.Fatalf("content 被改写: %s", prepared[0]["content"])
		}
	}
}

// TestInlineImageBytesKeepsSmallImage 覆盖未超限时的原样内联。
func TestInlineImageBytesKeepsSmallImage(t *testing.T) {
	data := encodePNG(t, noiseImage(16, 16))
	dataURL := inlineImageBytes(data, MaxInlineImageBytes, MaxInlineImageEdge)
	mime, decoded := splitDataURL(t, dataURL)
	if mime != "image/png" {
		t.Fatalf("mime = %q", mime)
	}
	if !bytes.Equal(decoded, data) {
		t.Fatal("未超限图片不应被重编码")
	}
}

// TestInlineImageBytesDownscalesOversizedImage 覆盖超限时降采样成 JPEG。
func TestInlineImageBytesDownscalesOversizedImage(t *testing.T) {
	data := encodePNG(t, noiseImage(900, 700))
	targetBytes := 200 * 1024
	if len(data) <= targetBytes {
		t.Fatalf("测试素材太小（%d 字节），无法触发降采样", len(data))
	}
	dataURL := inlineImageBytes(data, targetBytes, 320)
	mime, decoded := splitDataURL(t, dataURL)
	if mime != "image/jpeg" {
		t.Fatalf("mime = %q", mime)
	}
	if len(decoded) > targetBytes {
		t.Fatalf("降采样后 %d 字节，仍超过 %d", len(decoded), targetBytes)
	}
	frame, _, err := image.Decode(bytes.NewReader(decoded))
	if err != nil {
		t.Fatalf("降采样结果无法解码: %v", err)
	}
	if side := maxSide(frame); side > 320 {
		t.Fatalf("降采样后长边 = %d", side)
	}
}

// TestDownscaleImagePreservesAspectAndEdgeLimit 覆盖长边上限与等比缩放。
func TestDownscaleImagePreservesAspectAndEdgeLimit(t *testing.T) {
	data := encodePNG(t, noiseImage(2400, 800))
	shrunk, ok := downscaleImageBytes(data, MaxInlineImageBytes, MaxInlineImageEdge)
	if !ok {
		t.Fatal("降采样失败")
	}
	frame, _, err := image.Decode(bytes.NewReader(shrunk))
	if err != nil {
		t.Fatalf("结果无法解码: %v", err)
	}
	bounds := frame.Bounds()
	if bounds.Dx() != MaxInlineImageEdge {
		t.Fatalf("长边 = %d", bounds.Dx())
	}
	expectedHeight := float64(800) * float64(MaxInlineImageEdge) / float64(2400)
	if diff := float64(bounds.Dy()) - expectedHeight; diff > 1 || diff < -1 {
		t.Fatalf("短边 = %d, want ~%.1f", bounds.Dy(), expectedHeight)
	}
}

// TestDownscaleImageRejectsUndecodableData 覆盖无法解码时回退原字节。
func TestDownscaleImageRejectsUndecodableData(t *testing.T) {
	if _, ok := downscaleImageBytes([]byte("这不是图片"), MaxInlineImageBytes, MaxInlineImageEdge); ok {
		t.Fatal("无法解码的数据不应报告成功")
	}
	// 回退路径：认不出格式时按契约兜底 image/jpeg 原样内联。
	dataURL := inlineImageBytes([]byte("这不是图片"), 4, MaxInlineImageEdge)
	mime, decoded := splitDataURL(t, dataURL)
	if mime != "image/jpeg" {
		t.Fatalf("mime = %q", mime)
	}
	if string(decoded) != "这不是图片" {
		t.Fatal("无法降采样时应保留原始字节")
	}
}
