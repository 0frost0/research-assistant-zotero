package qa

import (
	"encoding/json"
	"testing"
)

func TestDefinitionAndFollowupScope(t *testing.T) {
	if definitionQuery("什么是EHR？") != "EHR definition 定义" {
		t.Fatal("definition hint missing")
	}
	if definitionQuery("什么是EHR，以及它的局限？") != "" {
		t.Fatal("multi-part scope stripped")
	}
	history := []Object{{"question": "MOTOR是什么？", "claims": []string{"history is not evidence"}}}
	query, turns := conversationContext(history, "它有多少参数？")
	if query != "MOTOR是什么？；它有多少参数？" || len(turns) != 1 {
		t.Fatal(query, turns)
	}
	query, turns = conversationContext(history, "RePrompT有哪些数据集？")
	if query != "RePrompT有哪些数据集？" || len(turns) != 0 {
		t.Fatal("unrelated question inherited context")
	}
	if !relevant("EHR", Object{"kind": "external_note", "quote": "EHR record", "score": json.Number("0.5")}) {
		t.Fatal("JSON score treated as zero")
	}
}
