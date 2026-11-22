package main

import (
	"bufio"
	"context"
	"flag"
	"io"
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
	managed := flag.Bool("managed-stdin", false, "Read private worker token line from stdin and stop when launcher closes the pipe")
	reranker := flag.String("reranker", "http://127.0.0.1:30002/rerank", "Project reranker URL")
	webStatic := flag.String("web-static", "", "Directory containing the shared knowledge workbench assets")
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
	stop, done := signal.NotifyContext(context.Background(), os.Interrupt)
	defer done()
	var internal []byte
	if *managed {
		if *workerToken != "" {
			log.Fatal("select only one worker token source")
		}
		input := bufio.NewReader(os.Stdin)
		internal, err = input.ReadSlice('\n')
		internal = append([]byte(nil), internal...)
		if err == nil {
			go func() {
				io.Copy(io.Discard, input)
				done()
			}()
		}
	} else {
		internal, err = os.ReadFile(*workerToken)
	}
	if err != nil || strings.TrimSpace(string(internal)) == "" {
		log.Fatal("worker token file unavailable")
	}
	// Acquire the product port before opening job storage. Opening it marks
	// unfinished jobs interrupted, which a second launcher must never do.
	listener, err := net.Listen("tcp", *bind)
	if err != nil {
		log.Fatal(err)
	}
	defer listener.Close()
	app, err := api.New(abs, strings.TrimSpace(string(token)), inference.New(*workerURL, strings.TrimSpace(string(internal))), *reranker)
	if err != nil {
		log.Fatal(err)
	}
	defer app.Close()
	server := &http.Server{Addr: *bind, Handler: app.Handler(*webStatic), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	go func() {
		<-stop.Done()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		server.Shutdown(ctx)
	}()
	log.Printf("Go product API listening on %s", *bind)
	if err = server.Serve(listener); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
