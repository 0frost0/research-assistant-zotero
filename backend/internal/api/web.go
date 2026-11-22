package api

import (
	"net/http"
	"path/filepath"
)

// Handler serves the shared knowledge workbench alongside the authenticated API.
// The asset list is fixed because the data root may contain private files.
func (s *Server) Handler(staticDir string) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/api/knowledge/", s)
	if staticDir == "" {
		return mux
	}
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/knowledge", http.StatusTemporaryRedirect)
	})
	for route, file := range map[string]string{
		"/knowledge":                   "knowledge.html",
		"/static/knowledge.css":        "knowledge.css",
		"/static/knowledge.js":         "knowledge.js",
		"/static/vendor/lucide.min.js": "vendor/lucide.min.js",
	} {
		mux.HandleFunc("GET "+route, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Referrer-Policy", "no-referrer")
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("X-Content-Type-Options", "nosniff")
			w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
			http.ServeFile(w, r, filepath.Join(staticDir, file))
		})
	}
	return mux
}
