// Command llm-transport 是 Sirius Pulse 的 LLM 传输侧车。
//
// 它只做四件事：对 AMKR 发 HTTP 请求、把 SSE 增量聚合成完整回复、把本地图片内联
// 成 data URL、按契约 §5.1 生成/编辑图片。编排、重试与 token 记账全部留在 Python 侧
// （契约见 CONTRACT.md）。
package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/contract"
	"github.com/Sparrived/SiriusPulse/go/llm-transport/internal/server"
)

const (
	// tokenEnv 是 token 的环境变量回退来源（契约 §1）。
	tokenEnv = "LLM_TRANSPORT_TOKEN"
	// listenAddress 固定回环地址：sidecar 不得监听 0.0.0.0（契约 §1）。
	listenAddress = "127.0.0.1"
	// shutdownWait 是退出时等待在飞请求的上限（契约 §1）。
	shutdownWait = 5 * time.Second
)

func main() {
	if err := run(); err != nil {
		log.New(os.Stderr, "[llm-transport] ", 0).Println("启动失败：" + err.Error())
		os.Exit(1)
	}
}

func run() error {
	flags := flag.NewFlagSet("llm-transport", flag.ContinueOnError)
	port := flags.Int("port", 0, "监听端口；0 表示由系统分配（实际端口见 READY 行）")
	token := flags.String("token", "", "Bearer token；缺省读环境变量 "+tokenEnv)
	showVersion := flags.Bool("version", false, "打印版本后退出")
	if err := flags.Parse(os.Args[1:]); err != nil {
		return err
	}
	if *showVersion {
		fmt.Fprintf(os.Stdout, "llm-transport %s\n", contract.Version)
		return nil
	}

	resolvedToken := strings.TrimSpace(*token)
	if resolvedToken == "" {
		resolvedToken = strings.TrimSpace(os.Getenv(tokenEnv))
	}
	if resolvedToken == "" {
		return fmt.Errorf("缺少 --token 或环境变量 %s", tokenEnv)
	}

	logger := log.New(os.Stderr, "[llm-transport] ", log.LstdFlags|log.Lmicroseconds)
	listener, err := net.Listen("tcp", fmt.Sprintf("%s:%d", listenAddress, *port))
	if err != nil {
		return err
	}
	actualPort, ok := listener.Addr().(*net.TCPAddr)
	if !ok {
		_ = listener.Close()
		return fmt.Errorf("无法解析监听地址：%s", listener.Addr())
	}

	// 长生成是常态，因此不设 ReadTimeout / WriteTimeout：只有块间静默超时由
	// upstream 包负责（契约 §4.2）。
	httpServer := &http.Server{
		Handler:           server.New(resolvedToken, contract.Version, logger).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	if err := announceReady(actualPort.Port); err != nil {
		_ = listener.Close()
		return err
	}
	logger.Printf("已监听 %s:%d，版本 %s", listenAddress, actualPort.Port, contract.Version)

	serveErrors := make(chan error, 1)
	go func() {
		serveErrors <- httpServer.Serve(listener)
	}()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)

	select {
	case serveErr := <-serveErrors:
		if serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			return serveErr
		}
		return nil
	case received := <-signals:
		logger.Printf("收到信号 %s，停止接收新请求（在飞请求最多等 %s）", received, shutdownWait)
	}

	shutdownContext, cancel := context.WithTimeout(context.Background(), shutdownWait)
	defer cancel()
	if err := httpServer.Shutdown(shutdownContext); err != nil {
		// 超时的在飞请求会被强制断开；契约只要求等待上限是 5s 且退出码为 0。
		logger.Printf("退出时仍有请求未完成：%v", err)
	}
	return nil
}

// announceReady 向 stdout 打印单行 READY <port> 并立即 flush（契约 §1）。
func announceReady(port int) error {
	writer := bufio.NewWriter(os.Stdout)
	if _, err := fmt.Fprintf(writer, "READY %d\n", port); err != nil {
		return err
	}
	return writer.Flush()
}
