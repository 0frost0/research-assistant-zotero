package main

import (
	"context"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/api"
	"github.com/0frost0/research-assistant-zotero/backend/internal/inference"
)

func main() {
	root := flag.String("root", ".", "Product data root")
	bind := flag.String("listen", "127.0.0.1:8776", "Loopback product address")
	workerURL := flag.String("worker", "http://127.0.0.1:8778", "Private inference worker URL")
	workerToken := flag.String("worker-token-file", "", "Private inference token file")
	reranker := flag.String("reranker", "http://127.0.0.1:30002/rerank", "Project reranker URL")
	flag.Parse()
	host, _, err := net.SplitHostPort(*bind)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		log.Fatal("listen must be an explicit loopback IP")
	}
	abs, err := filepath.Abs(*root)
	if err != nil {
		log.Fatal(err)
	}
	token, err := os.ReadFile(filepath.Join(abs, ".data", "zotero", "connection-token"))
	if err != nil {
		log.Fatal("connection token file unavailable")
	}
	internal, err := os.ReadFile(*workerToken)
	if err != nil || strings.TrimSpace(string(internal)) == "" {
		log.Fatal("worker token file unavailable")
	}
	app, err := api.New(abs, strings.TrimSpace(string(token)), inference.New(*workerURL, strings.TrimSpace(string(internal))), *reranker)
	if err != nil {
		log.Fatal(err)
	}
	defer app.Close()
	server := &http.Server{Addr: *bind, Handler: app, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	stop, done := signal.NotifyContext(context.Background(), os.Interrupt)
	defer done()
	go func() {
		<-stop.Done()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		server.Shutdown(ctx)
	}()
	log.Printf("Go product API listening on %s", *bind)
	if err = server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
