package credentials

import (
	"bytes"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"drmt/internal/config"
)

type RESTProxyTransport struct {
	ProxyURL string
	StaticIP string
	Client   *http.Client
}

type proxyRequestPayload struct {
	Method         string            `json:"method"`
	URL            string            `json:"url"`
	SourceIP       string            `json:"source_ip"`
	Headers        map[string]string `json:"headers"`
	Params         map[string]string `json:"params"`
	JSON           interface{}       `json:"json"`
	Data           interface{}       `json:"data"`
	Timeout        int               `json:"timeout"`
	AllowRedirects bool              `json:"allow_redirects"`
	VerifySSL      bool              `json:"verify_ssl"`
}

type proxyResponsePayload struct {
	StatusCode int               `json:"status_code"`
	Headers    map[string]string `json:"headers"`
	Text       string            `json:"text"`
	Content    string            `json:"content"`
	Error      interface{}       `json:"error,omitempty"`
}

func (t *RESTProxyTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if t.ProxyURL == "" || req.URL.String() == t.ProxyURL {
		return http.DefaultTransport.RoundTrip(req)
	}

	headers := make(map[string]string)
	var sourceIP string = t.StaticIP

	for k, v := range req.Header {
		if strings.EqualFold(k, "X-Static-IP") {
			if len(v) > 0 && v[0] != "" {
				sourceIP = v[0]
			}
			continue // Do not forward X-Static-IP to destination broker
		}
		if len(v) > 0 {
			headers[k] = v[0]
		}
	}

	params := make(map[string]string)
	for k, v := range req.URL.Query() {
		if len(v) > 0 {
			params[k] = v[0]
		}
	}

	reqURL := req.URL.String()
	if len(params) > 0 && strings.Contains(reqURL, "?") {
		reqURL = strings.SplitN(reqURL, "?", 2)[0]
	}

	var jsonBody interface{}
	var dataBody interface{}

	if req.Body != nil {
		bodyBytes, err := io.ReadAll(req.Body)
		if err == nil && len(bodyBytes) > 0 {
			req.Body = io.NopCloser(bytes.NewBuffer(bodyBytes)) // restore for caller
			
			var parsedJSON interface{}
			if err := json.Unmarshal(bodyBytes, &parsedJSON); err == nil {
				jsonBody = parsedJSON
			} else {
				dataBody = string(bodyBytes)
			}
		}
	}

	payload := proxyRequestPayload{
		Method:         strings.ToUpper(req.Method),
		URL:            reqURL,
		SourceIP:       sourceIP,
		Headers:        headers,
		Params:         params,
		JSON:           jsonBody,
		Data:           dataBody,
		Timeout:        config.HTTPTimeoutSeconds,
		AllowRedirects: true,
		VerifySSL:      true,
	}

	jsonBytes, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal proxy payload: %w", err)
	}

	proxyReq, err := http.NewRequestWithContext(req.Context(), "POST", t.ProxyURL, bytes.NewReader(jsonBytes))
	if err != nil {
		return nil, fmt.Errorf("create proxy request: %w", err)
	}
	proxyReq.Header.Set("Content-Type", "application/json")

	proxyResp, err := t.Client.Do(proxyReq)
	if err != nil {
		return nil, fmt.Errorf("proxy request failed: %w", err)
	}
	defer proxyResp.Body.Close()

	respBodyBytes, err := io.ReadAll(proxyResp.Body)
	if err != nil {
		return nil, fmt.Errorf("read proxy response: %w", err)
	}

	if proxyResp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("proxy failed (%d): %s", proxyResp.StatusCode, string(respBodyBytes))
	}

	var pResp proxyResponsePayload
	if err := json.Unmarshal(respBodyBytes, &pResp); err != nil {
		return nil, fmt.Errorf("unmarshal proxy response: %w", err)
	}

	if pResp.Error != nil && fmt.Sprintf("%v", pResp.Error) != "" && fmt.Sprintf("%v", pResp.Error) != "<nil>" {
		return nil, fmt.Errorf("request failed: %v", pResp.Error)
	}

	respBody := pResp.Text
	if respBody == "" {
		respBody = pResp.Content
	}

	resp := &http.Response{
		StatusCode: pResp.StatusCode,
		Status:     fmt.Sprintf("%d %s", pResp.StatusCode, http.StatusText(pResp.StatusCode)),
		Proto:      "HTTP/1.1",
		ProtoMajor: 1,
		ProtoMinor: 1,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(respBody)),
		Request:    req,
	}

	for k, v := range pResp.Headers {
		resp.Header.Set(k, v)
	}

	return resp, nil
}

func isRESTProxy(proxyURL string) bool {
	return strings.Contains(proxyURL, "/proxy")
}

func NewHTTPClient(proxyURL, staticIP string) *http.Client {
	if isRESTProxy(proxyURL) {
		rawClient := &http.Client{
			Timeout: config.HTTPTimeoutSeconds * time.Second,
		}
		return &http.Client{
			Transport: &RESTProxyTransport{
				ProxyURL: proxyURL,
				StaticIP: staticIP,
				Client:   rawClient,
			},
			Timeout: config.HTTPTimeoutSeconds * time.Second,
		}
	}

	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   10 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          100,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: false,
		},
	}

	if proxyURL != "" {
		proxyURLParsed, err := url.Parse(proxyURL)
		if err == nil {
			transport.Proxy = http.ProxyURL(proxyURLParsed)
		}
	}

	return &http.Client{
		Transport: transport,
		Timeout:   config.HTTPTimeoutSeconds * time.Second,
	}
}

func NewHTTPClientWithProxy(proxyURL string) *http.Client {
	return NewHTTPClient(proxyURL, "")
}