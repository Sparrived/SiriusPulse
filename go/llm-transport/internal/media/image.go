package media

import (
	"bytes"
	"image"
	"image/draw"
	"image/jpeg"
	"math"

	// 注册解码器：内联时按字节判定格式，需要 png/gif/jpeg 的 Decode 支持。
	_ "image/gif"
	_ "image/png"
)

// 契约 §5.2 的内联阈值与降采样参数。
const (
	// MaxInlineImageBytes 是单张内联图片的字节上限（压缩前判定阈值与压缩目标共用）。
	MaxInlineImageBytes = 10 * 1024 * 1024
	// MaxInlineImageEdge 是降采样后的长边像素上限，超过的部分对视觉编码器纯属浪费。
	MaxInlineImageEdge = 1568
)

// jpegQualityLadder 是逐级下调的 JPEG 质量，先试高画质，超限再降。
var jpegQualityLadder = []int{85, 70, 55, 40}

// shrinkFactor 是质量降到底仍超限时继续缩小长边的比例。
const shrinkFactor = 0.75

// minEdge 是长边缩小的下限：再小就会丢失可辨识的细节，不如接受超限。
const minEdge = 320

// defaultImageMime 是魔数认不出时的兜底媒体类型（契约 §5.2）。
const defaultImageMime = "image/jpeg"

// imageMagicPrefixes 是图片容器的魔数前缀。
//
// 媒体类型必须按字节判定：缓存常把各种图片统一存成 .jpg，内容却可能是 PNG，
// 上游按字节校验声明，声明错了整个请求会被拒。
var imageMagicPrefixes = []struct {
	prefix []byte
	mime   string
}{
	{[]byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a}, "image/png"},
	{[]byte{0xff, 0xd8, 0xff}, "image/jpeg"},
	{[]byte("GIF87a"), "image/gif"},
	{[]byte("GIF89a"), "image/gif"},
}

// DetectImageMime 按魔数判定图片媒体类型；第二个返回值表示是否认出。
//
// 与 SniffImageMime 的差别只在「认不出」时：生图链路（契约 §5.1.2.7）在魔数之后
// 还要按后缀猜、再兜底 image/png，因此需要区分「认出来是 JPEG」与「根本没认出」。
func DetectImageMime(data []byte) (string, bool) {
	for _, magic := range imageMagicPrefixes {
		if bytes.HasPrefix(data, magic.prefix) {
			return magic.mime, true
		}
	}
	if len(data) >= 12 && bytes.Equal(data[:4], []byte("RIFF")) && bytes.Equal(data[8:12], []byte("WEBP")) {
		return "image/webp", true
	}
	return "", false
}

// SniffImageMime 按魔数判定图片媒体类型，认不出时回退 image/jpeg。
func SniffImageMime(data []byte) string {
	if mime, ok := DetectImageMime(data); ok {
		return mime
	}
	return defaultImageMime
}

// downscaleImageBytes 把过大的图片降采样成 JPEG 字节（契约 §5.2）。
//
// 先按长边上限缩略，再沿质量阶梯下压；若仅降质量仍超过 maxBytes，继续按
// shrinkFactor 缩小长边（不低于 minEdge）。目标是尽量让图片留在内联通道里——
// 丢图意味着模型彻底看不到它。解码失败（例如 stdlib 没有 WEBP 解码器）时返回
// false，调用方按原字节内联。
func downscaleImageBytes(data []byte, maxBytes int, maxEdge int) ([]byte, bool) {
	decoded, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, false
	}
	frame := resizeToMaxEdge(decoded, maxEdge)
	if frame == nil {
		return nil, false
	}

	edge := maxEdge
	var smallest []byte
	for {
		for _, quality := range jpegQualityLadder {
			encoded, encodeErr := encodeJPEG(frame, quality)
			if encodeErr != nil {
				return nil, false
			}
			if smallest == nil || len(encoded) < len(smallest) {
				smallest = encoded
			}
			if len(encoded) <= maxBytes {
				return encoded, true
			}
		}
		nextEdge := int(float64(edge) * shrinkFactor)
		if nextEdge < minEdge || maxSide(frame) <= minEdge {
			// 已经缩到下限仍超限：返回最小的一份，仍好过丢图。
			return smallest, smallest != nil
		}
		edge = nextEdge
		shrunk := resizeToMaxEdge(frame, edge)
		if shrunk == nil {
			return smallest, smallest != nil
		}
		frame = shrunk
	}
}

// resizeToMaxEdge 把图片等比缩到长边不超过 maxEdge（不放大）。
func resizeToMaxEdge(src image.Image, maxEdge int) *image.RGBA {
	bounds := src.Bounds()
	width, height := bounds.Dx(), bounds.Dy()
	if width <= 0 || height <= 0 || maxEdge <= 0 {
		return nil
	}
	longEdge := width
	if height > longEdge {
		longEdge = height
	}
	if longEdge <= maxEdge {
		canvas := image.NewRGBA(image.Rect(0, 0, width, height))
		draw.Draw(canvas, canvas.Bounds(), src, bounds.Min, draw.Src)
		return canvas
	}
	scale := float64(maxEdge) / float64(longEdge)
	targetWidth := maxInt(1, int(math.Round(float64(width)*scale)))
	targetHeight := maxInt(1, int(math.Round(float64(height)*scale)))
	canvas := image.NewRGBA(image.Rect(0, 0, targetWidth, targetHeight))
	for targetY := 0; targetY < targetHeight; targetY++ {
		sourceY0, sourceY1 := sourceRange(targetY, targetHeight, height)
		for targetX := 0; targetX < targetWidth; targetX++ {
			sourceX0, sourceX1 := sourceRange(targetX, targetWidth, width)
			var red, green, blue, count uint64
			for sourceY := sourceY0; sourceY < sourceY1; sourceY++ {
				for sourceX := sourceX0; sourceX < sourceX1; sourceX++ {
					pixelRed, pixelGreen, pixelBlue, _ := src.At(bounds.Min.X+sourceX, bounds.Min.Y+sourceY).RGBA()
					red += uint64(pixelRed >> 8)
					green += uint64(pixelGreen >> 8)
					blue += uint64(pixelBlue >> 8)
					count++
				}
			}
			if count == 0 {
				count = 1
			}
			offset := canvas.PixOffset(targetX, targetY)
			canvas.Pix[offset] = uint8(red / count)
			canvas.Pix[offset+1] = uint8(green / count)
			canvas.Pix[offset+2] = uint8(blue / count)
			canvas.Pix[offset+3] = 0xff
		}
	}
	return canvas
}

// sourceRange 给出目标像素在源图上覆盖的半开区间，保证每个源像素至少被用到一次。
func sourceRange(target int, targetSize int, sourceSize int) (int, int) {
	start := int(float64(target) * float64(sourceSize) / float64(targetSize))
	end := int(float64(target+1) * float64(sourceSize) / float64(targetSize))
	if end <= start {
		end = start + 1
	}
	if end > sourceSize {
		end = sourceSize
	}
	if start >= sourceSize {
		start = sourceSize - 1
	}
	return start, end
}

// encodeJPEG 按指定质量编码 JPEG。
func encodeJPEG(frame image.Image, quality int) ([]byte, error) {
	var buffer bytes.Buffer
	if err := jpeg.Encode(&buffer, frame, &jpeg.Options{Quality: quality}); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

// maxSide 返回图片长边像素数。
func maxSide(frame image.Image) int {
	bounds := frame.Bounds()
	width, height := bounds.Dx(), bounds.Dy()
	if height > width {
		return height
	}
	return width
}

// maxInt 返回两个整数中的较大值。
func maxInt(left int, right int) int {
	if left > right {
		return left
	}
	return right
}
