package store

import (
	"bytes"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

type Object = map[string]any

var ErrConflict = errors.New("revision_conflict")
var ErrInvalid = errors.New("invalid_request")
var validKey = regexp.MustCompile(`^[A-Za-z0-9_.:-]+$`)

func Now() string { return time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00") }

// Canonical matches Python ensure_ascii=False, sort_keys=True, separators=(",", ":").
// Store identity/fingerprint inputs contain only strings, integers, booleans and null.
func Canonical(v any) string {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	if err := e.Encode(v); err != nil {
		panic(err)
	}
	s := strings.TrimSuffix(b.String(), "\n")
	// encoding/json escapes these even with HTML escaping disabled.
	var out strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+1 < len(s) {
			if strings.HasPrefix(s[i:], `\u2028`) {
				out.WriteRune('\u2028')
				i += 5
				continue
			}
			if strings.HasPrefix(s[i:], `\u2029`) {
				out.WriteRune('\u2029')
				i += 5
				continue
			}
			out.WriteByte(s[i])
			i++
			out.WriteByte(s[i])
			continue
		}
		out.WriteByte(s[i])
	}
	return out.String()
}

func Decode(raw string) (Object, error) {
	var v Object
	d := json.NewDecoder(strings.NewReader(raw))
	d.UseNumber()
	err := d.Decode(&v)
	if err == nil {
		var extra any
		if next := d.Decode(&extra); next != io.EOF {
			err = ErrInvalid
		}
	}
	if err == nil && v == nil {
		err = ErrInvalid
	}
	return v, err
}
func Hash(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func Identity(parts ...string) string {
	ns, _ := hex.DecodeString("6ba7b8119dad11d180b400c04fd430c8")
	h := sha1.New()
	h.Write(ns)
	h.Write([]byte(Canonical(parts)))
	b := h.Sum(nil)[:16]
	b[6] = b[6]&15 | 0x50
	b[8] = b[8]&63 | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}
func Text(v any, limit int, empty bool) (string, error) {
	s, ok := v.(string)
	if !ok || utf8.RuneCountInString(s) > limit || (!empty && strings.TrimSpace(s) == "") {
		return "", ErrInvalid
	}
	return strings.TrimSpace(s), nil
}
func Key(v any) (string, error) {
	s, err := Text(v, 128, false)
	if err != nil || !validKey.MatchString(s) {
		return "", ErrInvalid
	}
	return s, nil
}
func Int(v any) (int64, bool) {
	switch n := v.(type) {
	case int:
		return int64(n), true
	case int64:
		return n, true
	case json.Number:
		i, e := n.Int64()
		return i, e == nil
	default:
		return 0, false
	}
}
func String(v any) string { s, _ := v.(string); return s }
func Default(m Object, k string, fallback any) any {
	if v, ok := m[k]; ok {
		return v
	}
	return fallback
}
