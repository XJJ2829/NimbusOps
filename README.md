# NimbusOps

NimbusOps 是面向复杂运维场景的安全智能运维 Agent 项目。项目提供 WebUI 与服务端运行时，并以 Kubernetes 和主机运维基准环境验证故障诊断、操作安全控制和结果评测能力。

## 项目结构

| 目录 | 说明 |
| --- | --- |
| [`My-agent`](./My-agent) | NimbusOps 主服务源码：Python 服务端、Agent 运行时、安全策略与 React WebUI。 |
| [`K8S Bench`](./K8S%20Bench) | AIOpsLab Kubernetes 故障现场生成与评测环境。 |
| [`HostOpsBench`](./HostOpsBench) | 主机运维场景测试框架，含故障注入、调用轨迹和评分报告。 |
| 决赛演示视频 | 本 README 中可直接播放。 |

## 快速开始

### 运行主服务

需要 Python 3.11 或 3.12，以及 Node.js/npm（用于 WebUI）。

```bash
cd My-agent
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
my-agent init-config
my-agent serve
```

服务默认监听 `http://127.0.0.1:18790`。

开发 WebUI：

```bash
cd My-agent/webui
npm ci
npm run dev
```

默认开发地址为 `http://127.0.0.1:5173`。

## 测试与评测

- Kubernetes 场景使用 [`K8S Bench/README.md`](./K8S%20Bench/README.md) 中的 AIOpsLab 工作流；它负责部署真实微服务、注入故障、生成负载并执行评测。
- 主机运维场景使用 [`HostOpsBench/README.md`](./HostOpsBench/README.md)，覆盖 IO 高负载、日志洪泛、CPU 饱和、文件描述符泄漏、上游 HTTP 故障及 systemd 配置错误等场景。

## 决赛演示视频

演示视频：

https://github.com/user-attachments/assets/70a5da70-d36d-42d0-9c52-24e92a4fecb3

## 安全说明

项目中用于本地测试的 TLS 私钥和私有证书不纳入仓库，需通过部署环境的密钥管理方式提供。
