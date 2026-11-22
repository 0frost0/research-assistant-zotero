package inference

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

type Client struct {
	URL, Token string
	HTTP       *http.Client
}

func New(url, token string) *Client {
	return &Client{URL: url, Token: token, HTTP: &http.Client{Timeout: 60 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
}
func (c *Client) Call(ctx context.Context, connection, operation string, data store.Object) (store.Object, error) {
	data["connection"] = connection
	raw := []byte(store.Canonical(data))
	r, e := http.NewRequestWithContext(ctx, "POST", c.URL+"/internal/"+operation, bytes.NewReader(raw))
	if e != nil {
		return nil, e
	}
	r.Header.Set("Authorization", "Bearer "+c.Token)
	r.Header.Set("Content-Type", "application/json")
	client := c.HTTP
	if operation == "parse" {
		copy := *client
		copy.Timeout = 0
		client = &copy
	}
	response, e := client.Do(r)
	if e != nil {
		return nil, e
	}
	defer response.Body.Close()
	if operation == "search" && response.StatusCode == http.StatusConflict {
		return nil, store.ErrConflict
	}
	if response.StatusCode != 200 && response.StatusCode != 202 {
		return nil, fmt.Errorf("inference_http_%d", response.StatusCode)
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 36*1024*1024))
	decoder.UseNumber()
	var result store.Object
	if e = decoder.Decode(&result); e != nil {
		return nil, e
	}
	return result, nil
}
