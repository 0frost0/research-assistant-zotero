package qa

import (
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

var definitionPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)^什么是\s*(.+)$`),
	regexp.MustCompile(`(?i)^(.+?)(?:是什么|是指什么|的定义(?:是什么)?)$`),
	regexp.MustCompile(`(?i)^what (?:is|are)\s+(.+)$`),
	regexp.MustCompile(`(?i)^(?:define|definition of)\s+(.+)$`),
}
var definitionPrefix = regexp.MustCompile(`^(?:请问|请解释(?:一下)?|请介绍(?:一下)?)\s*`)
var multiQuestion = regexp.MustCompile(`[，,；;？?]|以及|并且`)
var followup = regexp.MustCompile(`(?i)(?:\b(?:it|this|that|they|the method|the study)\b|它|这(?:篇|个|项|种)|该(?:方法|研究|模型|论文)|上述|那篇)`)

func definitionQuery(question string) string {
	q := strings.TrimSpace(strings.TrimRight(strings.TrimSpace(question), "？?。.!！"))
	q = definitionPrefix.ReplaceAllString(q, "")
	for _, pattern := range definitionPatterns {
		match := pattern.FindStringSubmatch(q)
		if len(match) != 2 {
			continue
		}
		subject := strings.TrimSpace(match[1])
		if utf8.RuneCountInString(subject) <= 1 || utf8.RuneCountInString(subject) > 120 || multiQuestion.MatchString(subject) {
			continue
		}
		terms := []string{subject}
		switch subject {
		case "心衰":
			terms = append(terms, "心力衰竭", "heart failure")
		case "心力衰竭":
			terms = append(terms, "心衰", "heart failure")
		case "痴呆":
			terms = append(terms, "dementia")
		case "阿尔茨海默":
			terms = append(terms, "alzheimer dementia")
		}
		return strings.Join(terms, " ") + " definition 定义"
	}
	return ""
}
func conversationContext(history any, question string) (string, []Object) {
	turns := []Object{}
	if !followup.MatchString(question) {
		return question, turns
	}
	raw := array(history)
	if len(raw) > 3 {
		raw = raw[len(raw)-3:]
	}
	for _, item := range raw {
		q := strings.TrimSpace(store.String(item["question"]))
		if q == "" {
			continue
		}
		runes := []rune(q)
		if len(runes) > 500 {
			q = string(runes[:500])
		}
		claims := list(item["claims"])
		if len(claims) > 3 {
			claims = claims[:3]
		}
		turns = append(turns, Object{"question": q, "claims": claims})
	}
	if len(turns) == 0 {
		return question, turns
	}
	q := store.String(turns[len(turns)-1]["question"]) + "；" + question
	runes := []rune(q)
	if len(runes) > 1000 {
		q = string(runes[:1000])
	}
	return q, turns
}
