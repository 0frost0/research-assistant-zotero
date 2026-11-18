package api

import (
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"unicode"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

func serviceURL(v any) (string, error) {
	s, err := store.Text(v, 2000, false)
	if err != nil {
		return "", err
	}
	u, err := url.Parse(s)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || strings.IndexFunc(s, unicode.IsSpace) >= 0 {
		return "", store.ErrInvalid
	}
	local := u.Hostname() == "localhost" || u.Hostname() == "127.0.0.1" || u.Hostname() == "::1"
	if u.Scheme != "https" && !(u.Scheme == "http" && local) {
		return "", store.ErrInvalid
	}
	return strings.TrimRight(s, "/"), nil
}
func (s *Server) saveConfig(connection string, d Object) (Object, error) {
	if connection == "" {
		return nil, store.ErrInvalid
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	old, err := s.config(connection)
	if err != nil {
		return nil, err
	}
	clean := Object{}
	for _, name := range []string{"llm", "embedding", "mineru"} {
		v, ok := d[name].(map[string]any)
		if !ok {
			return nil, store.ErrInvalid
		}
		p := Object{}
		if name != "mineru" {
			p["model"], err = store.Text(v["model"], 200, false)
			if err != nil {
				return nil, err
			}
			base, err := serviceURL(v["base_url"])
			if err != nil {
				return nil, err
			}
			if name == "llm" {
				base = strings.TrimSuffix(base, "/chat/completions")
			}
			p["base_url"] = base
			key, err := store.Text(store.Default(v, "api_key", ""), 8192, true)
			if err != nil {
				return nil, err
			}
			previous, _ := old[name].(map[string]any)
			previousURL := store.String(previous["base_url"])
			if name == "llm" {
				previousURL = strings.TrimSuffix(strings.TrimRight(previousURL, "/"), "/chat/completions")
			}
			if key == "" {
				if previousURL != base {
					return nil, store.ErrInvalid
				}
				key = store.String(previous["api_key"])
			}
			if key == "" {
				return nil, store.ErrInvalid
			}
			p["api_key"] = key
			if name == "llm" {
				images, ok := store.Default(v, "supports_images", false).(bool)
				if !ok {
					return nil, store.ErrInvalid
				}
				p["supports_images"] = images
			} else {
				dimension, ok := store.Int(store.Default(v, "dimension", 2048))
				if !ok || dimension < 1 || dimension > 8192 {
					return nil, store.ErrInvalid
				}
				p["dimension"] = dimension
				p["revision"], err = store.Text(store.Default(v, "revision", "1"), 100, false)
				if err != nil {
					return nil, err
				}
			}
		} else {
			mode := store.Default(v, "mode", "remote")
			if mode != "local" && mode != "remote" {
				return nil, store.ErrInvalid
			}
			p["mode"] = mode
			p["url"] = store.Default(v, "url", "")
			if mode == "remote" {
				p["url"], err = serviceURL(p["url"])
				if err != nil {
					return nil, err
				}
			}
			timeout, ok := store.Int(store.Default(v, "timeout", 1800))
			if !ok || timeout < 60 || timeout > 7200 {
				return nil, store.ErrInvalid
			}
			p["timeout"] = timeout
			python, ok := store.Default(v, "python", "").(string)
			if !ok || python != "" && !filepath.IsAbs(python) {
				return nil, store.ErrInvalid
			}
			p["python"] = python
			auto, ok := store.Default(v, "auto_parse", false).(bool)
			if !ok {
				return nil, store.ErrInvalid
			}
			p["auto_parse"] = auto
		}
		clean[name] = p
	}
	path := filepath.Join(s.Store.Home, "service-config.json")
	all := Object{}
	raw, err := os.ReadFile(path)
	if err == nil {
		all, err = store.Decode(string(raw))
	}
	if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	all[connection] = clean
	f, err := os.CreateTemp(s.Store.Home, "service-config-*.tmp")
	if err != nil {
		return nil, err
	}
	defer os.Remove(f.Name())
	if _, err = f.WriteString(store.Canonical(all)); err != nil {
		f.Close()
		return nil, err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return nil, err
	}
	if err = f.Close(); err != nil {
		return nil, err
	}
	if err = os.Rename(f.Name(), path); err != nil {
		return nil, err
	}
	return publicConfig(clean), nil
}
