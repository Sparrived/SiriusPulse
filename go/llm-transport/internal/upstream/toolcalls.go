package upstream

import (
	"encoding/json"
	"sort"
	"strconv"
	"strings"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
)

// toolCallEntry 是一条工具调用在流式过程中的累加状态。
type toolCallEntry struct {
	id   string
	typ  string
	name string
	args string
}

// toolCallAccumulator 按 index 归位的流式 tool_calls 累加器（契约 §4.4）。
type toolCallAccumulator map[int]*toolCallEntry

// merge 把一帧 delta.tool_calls 并进累加器。
//
// 流式协议里 function.name 与 function.arguments 都是跨块拼接的，id/type 只在
// 首块出现：id/type 覆盖，name/arguments 累加。
func (a toolCallAccumulator) merge(raw json.RawMessage) {
	if isNullJSON(raw) {
		return
	}
	var list []json.RawMessage
	if err := json.Unmarshal(raw, &list); err != nil {
		return
	}
	for position, item := range list {
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(item, &fields); err != nil {
			continue
		}
		index := position
		if rawIndex, ok := fields["index"]; ok {
			if parsed, ok := intValue(rawIndex); ok {
				index = parsed
			}
		}
		entry := a[index]
		if entry == nil {
			entry = &toolCallEntry{typ: "function"}
			a[index] = entry
		}
		if id := stringField(fields["id"]); id != "" {
			entry.id = id
		}
		if typ := stringField(fields["type"]); typ != "" {
			entry.typ = typ
		}
		if functionRaw, ok := fields["function"]; ok {
			var function map[string]json.RawMessage
			if err := json.Unmarshal(functionRaw, &function); err != nil {
				continue
			}
			entry.name += stringField(function["name"])
			entry.args += stringField(function["arguments"])
		}
	}
}

// finalize 按 index 升序输出聚合结果；无条目时返回空切片（契约 §3 要求 [] 而非 null）。
func (a toolCallAccumulator) finalize() []contract.ToolCall {
	calls := make([]contract.ToolCall, 0, len(a))
	indexes := make([]int, 0, len(a))
	for index := range a {
		indexes = append(indexes, index)
	}
	sort.Ints(indexes)
	for _, index := range indexes {
		entry := a[index]
		typ := entry.typ
		if typ == "" {
			typ = "function"
		}
		calls = append(calls, contract.ToolCall{
			ID:                entry.id,
			Type:              typ,
			FunctionName:      entry.name,
			FunctionArguments: entry.args,
		})
	}
	return calls
}

// parseToolCalls 解析非流式响应里的整段 tool_calls。
func parseToolCalls(raw json.RawMessage) []contract.ToolCall {
	calls := make([]contract.ToolCall, 0)
	if isNullJSON(raw) {
		return calls
	}
	var list []json.RawMessage
	if err := json.Unmarshal(raw, &list); err != nil {
		return calls
	}
	for _, item := range list {
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(item, &fields); err != nil {
			continue
		}
		call := contract.ToolCall{Type: "function"}
		call.ID = stringField(fields["id"])
		if typ := stringField(fields["type"]); typ != "" {
			call.Type = typ
		}
		if functionRaw, ok := fields["function"]; ok {
			var function map[string]json.RawMessage
			if err := json.Unmarshal(functionRaw, &function); err == nil {
				call.FunctionName = stringField(function["name"])
				call.FunctionArguments = stringField(function["arguments"])
			}
		}
		calls = append(calls, call)
	}
	return calls
}

// intValue 尽力把一个 JSON 值转成整数（对应 Python 的 int(raw_index)）。
func intValue(raw json.RawMessage) (int, bool) {
	if len(raw) == 0 {
		return 0, false
	}
	var number float64
	if err := json.Unmarshal(raw, &number); err == nil {
		return int(number), true
	}
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		parsed, convErr := strconv.Atoi(strings.TrimSpace(text))
		if convErr == nil {
			return parsed, true
		}
	}
	return 0, false
}
