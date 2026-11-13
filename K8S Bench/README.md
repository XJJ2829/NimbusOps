# AIOpsLab 终端测试环境

这是本项目改造后的 AIOpsLab 使用说明。当前版本不把 AIOpsLab 当作完整 Agent
框架使用，而是把它作为可重复的故障现场生成器和评测器，配合 NimbusOps
完成真实 Kubernetes 运维场景测试。

本 README 基于以下两份项目文档整理：

- `AIOPSLAB终端测试工作流.md`
- `AIOPSLAB测试执行手册.md`

## 项目定位

完整链路如下：

1. 在终端启动一个 AIOpsLab 真实故障场景。
2. AIOpsLab 部署微服务、注入故障、启动业务 workload。
3. 用户只用自然语言告诉 NimbusOps 当前环境存在故障。
4. NimbusOps 使用自己的 Kubernetes、日志、指标和 Trace 工具排查现场。
5. NimbusOps 对 mitigation 任务直接修复集群，对其他任务输出诊断答案。
6. 用户在终端运行 AIOpsLab 评测脚本。
7. 用户清理本轮场景，或复位同一故障用于下一轮演示。

这里的 AIOpsLab 不负责运行 NimbusOps 的 Agent Loop，也不把 AIOpsLab 自带的
Baseline Agent 工具注册给 NimbusOps。AIOpsLab 的角色是：

- 部署真实微服务应用。
- 创建 Kubernetes namespace、Pod、Deployment、Service、ConfigMap、PVC 等资源。
- 准备 OpenEBS、Prometheus 等平台组件。
- 注入真实故障。
- 生成 workload。
- 根据现场状态执行评测。

## 目录约定

两个项目位于同一级目录：

```text
Harness-Agent/
├── AIOpsLab-main/
└── My-agent/
```

日常测试命令从 NimbusOps 项目目录执行：

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/My-agent
```

`My-agent/scripts` 中的 AIOpsLab 终端脚本默认寻找：

```text
../AIOpsLab-main
```

如果移动了 AIOpsLab，可以显式配置：

```bash
export AIOPSLAB_HOME=/实际路径/AIOpsLab-main
export AIOPSLAB_PYTHON=/实际路径/AIOpsLab-main/.venv/bin/python
```

## 改造重点

当前工程相对原始 AIOpsLab 的重点变化：

- 使用 `My-agent/scripts/aiopslab_start.py` 启动真实故障场景。
- 使用 `My-agent/scripts/aiopslab_eval.py` 独立评测当前场景。
- 使用 `My-agent/scripts/aiopslab_cleanup.py` 分层清理场景和平台环境。
- 默认 cleanup 只清理实验场景，保留 kind、OpenEBS、observe、Prometheus 和 Helm 平台组件。
- 新增 `--reset-scenario`，用于删除业务 namespace、重新部署 app、重新注入同一故障，适合反复演示。
- `KubeCtl` 支持 kind 默认 context，也支持通过 `AIOPSLAB_CLUSTER` 指定并行 kind 集群 context。
- workload Job 的完成状态被视为就绪，避免 wrk2 这类 Job 完成后阻塞就绪等待。
- AIOpsLab 使用独立 `.venv`，终端脚本会自动切换到 AIOpsLab 自己的 Python 环境。

## 首次环境配置

### 1. 安装本地依赖

本地 kind 测试至少需要：

- Python 3.11
- Docker Desktop 或 OrbStack
- kubectl
- kind
- Helm

macOS 可以执行：

```bash
brew install kind helm kubectl python@3.11
```

本工作流不运行 AIOpsLab 自带 Baseline Agent，因此不强制安装 Poetry，也不需要配置
GPT、Qwen、DeepSeek 等 API Key。

### 2. 启动容器运行时

kind 需要正在运行的 Docker 兼容容器环境。启动 Docker Desktop 或 OrbStack 后检查：

```bash
docker info
```

如果本机有多个 Docker context，可以检查并切换：

```bash
docker context ls
docker context use <正在运行的-context>
```

### 3. 补齐应用子模块

`aiopslab-applications/` 必须有真实应用内容：

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main
git submodule update --init --recursive
```

验证目录不是空的：

```bash
find aiopslab-applications -maxdepth 2 -type f | head
```

如果当前代码不是通过 Git clone 得到的，无法使用 submodule 命令，可以直接克隆：

```bash
git clone https://github.com/xlab-uiuc/aiopslab-applications.git aiopslab-applications
```

执行直接克隆前，要确保目标目录为空。

### 4. 创建 AIOpsLab 独立 Python 环境

建议不要把 AIOpsLab 依赖装进 NimbusOps 的虚拟环境：

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main
/opt/homebrew/bin/python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
deactivate
```

之后即使从 NimbusOps 的虚拟环境执行终端脚本，脚本也会自动切换到：

```text
AIOpsLab-main/.venv/bin/python
```

### 5. 创建 AIOpsLab 配置

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main
cp aiopslab/config.yml.example aiopslab/config.yml
```

本地 kind 环境建议：

```yaml
k8s_host: kind
k8s_user: xujunjie
ssh_key_path: ~/.ssh/id_rsa
data_dir: data
qualitative_eval: false
print_session: false
```

`qualitative_eval` 建议保持 `false`，这样终端评测不会调用额外大模型。

### 6. 创建 Apple Silicon kind 集群

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main
kind create cluster --config kind/kind-config-arm.yaml
```

验证：

```bash
kubectl config current-context
kubectl get nodes
kubectl get namespaces
```

预期 context 类似 `kind-kind`，节点最终应为 `Ready`。

## 标准测试流程

所有命令从 `My-agent` 目录执行：

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/My-agent
```

### 1. 启动故障场景

以 Hotel Reservation 修复任务为例：

```bash
python scripts/aiopslab_start.py misconfig_app_hotel_res-mitigation-1
```

启动脚本会执行：

1. 检查 AIOpsLab 目录、`config.yml` 和应用子模块。
2. 自动使用 AIOpsLab 的 `.venv`。
3. 为 Kubernetes 场景准备 OpenEBS 和 Prometheus。
4. 清理同名应用的旧资源。
5. 部署真实微服务应用并等待资源就绪。
6. 注入该 problem id 对应的故障。
7. 启动该场景定义的 workload。
8. 将当前测试信息写入 `.aiopslab-terminal/state.json`。

终端显示 `AIOpsLab fault scenario is running` 后，再开始和 NimbusOps 对话。

### 2. 给 NimbusOps 真实运维请求

不要告诉 NimbusOps：

- AIOpsLab problem id。
- 测试 namespace。
- 故障类型。
- 故障组件。
- AIOpsLab registry 或 problem 源码。
- 评测器中的标准答案。

推荐 prompt 见下文“Prompt 模板”。

### 3. 评测

Mitigation 任务直接评测现场是否恢复：

```bash
python scripts/aiopslab_eval.py
```

Detection、Localization、Analysis 任务需要把 NimbusOps 的最终答案传给 `--solution`。

评测结果中的关键字段：

```json
{
  "success": true
}
```

`success: true` 表示通过。评测不会自动清理现场，如果失败，可以继续让
NimbusOps 修复或补充诊断，然后再次运行评测。

### 4. 清理或复位

日常实验结束：

```bash
python scripts/aiopslab_cleanup.py
```

默认只清理当前 scenario，保留平台基础设施，并输出：

```text
Cleaning scenario resources only. Platform infrastructure will be preserved.
```

演示下一轮，恢复同一个故障：

```bash
python scripts/aiopslab_cleanup.py --reset-scenario
```

该模式会删除并重建当前业务场景，重新部署 app，重新注入同一个 fault；如果原
scenario 启动过 workload，也会重新启动 workload。

真正重装环境：

```bash
python scripts/aiopslab_cleanup.py --full
```

该模式会删除业务场景、Prometheus、observe、OpenEBS、storageclass 等平台组件，并输出：

```text
Full cleanup enabled. Removing AIOpsLab platform components.
```

## Prompt 模板

### Mitigation

```text
当前运行环境存在服务故障，请排查根因并完成修复，使服务恢复正常。不要假设故障类型，先观察系统状态、日志和事件；修复后说明你做了什么。
```

评测：

```bash
python scripts/aiopslab_eval.py
```

### Localization

```text
当前运行环境存在服务故障，请排查并定位根因所在组件，最后只输出组件名列表，例如 ["frontend"]。
```

评测：

```bash
python scripts/aiopslab_eval.py --solution '["<组件名>"]'
```

### Analysis

```text
当前运行环境存在服务故障，请分析根因所属层级和故障类型，最后只输出 JSON，例如 {"system_level":"Application","fault_type":"Misconfiguration"}。
```

评测：

```bash
python scripts/aiopslab_eval.py --solution '{"system_level":"<系统层级>","fault_type":"<故障类型>"}'
```

### Detection

```text
当前运行环境可能存在服务故障，请排查系统状态、日志和事件，最后只回答是否存在故障：Yes 或 No。
```

评测：

```bash
python scripts/aiopslab_eval.py --solution '"Yes"'
python scripts/aiopslab_eval.py --solution '"No"'
```

## 常用命令

启动默认场景：

```bash
python scripts/aiopslab_start.py
```

启动指定场景：

```bash
python scripts/aiopslab_start.py <problem_id>
```

只部署并注入故障，不启动 workload：

```bash
python scripts/aiopslab_start.py <problem_id> --no-workload
```

平台已经准备好时，跳过 OpenEBS 和 Prometheus 准备：

```bash
python scripts/aiopslab_start.py <problem_id> --skip-platform-setup
```

保留应用资源，仅恢复故障：

```bash
python scripts/aiopslab_cleanup.py --keep-app
```

保留本地状态文件：

```bash
python scripts/aiopslab_cleanup.py --keep-state
```

这些参数主要用于调试，正式演示推荐使用默认启动、默认清理、`--reset-scenario`
和 `--full`。

## 运行现场在哪里

真实状态主要在 kind Kubernetes 集群中，包括：

- 应用 namespace。
- Pod 状态。
- Deployment 和 Service 配置。
- ConfigMap、Secret、PVC 等资源。
- Kubernetes Event。
- Pod 日志。
- workload Job 的运行结果。

常见 namespace：

```text
test-hotel-reservation
test-social-network
astronomy-shop
observe
default
openebs
```

观察现场：

```bash
kubectl get ns
kubectl get pods -A
kubectl get svc -A
kubectl get events -A --sort-by=.lastTimestamp
```

查看业务日志：

```bash
kubectl logs -n test-hotel-reservation <pod-name>
kubectl describe pod -n test-hotel-reservation <pod-name>
```

wrk2 workload 通常在 `default` namespace：

```bash
kubectl get jobs -n default
kubectl logs -n default job/wrk2-job
```

## 本机端口和输出文件

业务服务通常不直接暴露在 Mac 本机端口上。以 Hotel Reservation 为例，workload
由集群里的 `wrk2-job` 访问服务 ClusterIP：

```text
http://<frontend-cluster-ip>:5000
```

Prometheus 通常通过：

```text
http://localhost:32000
```

背后是：

```text
namespace: observe
service: prometheus-server
```

Jaeger Trace 通常通过：

```text
http://localhost:16686
```

本地状态文件：

```text
/Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/My-agent/.aiopslab-terminal/state.json
```

如果显式采集 metrics，会导出到：

```text
/Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main/metrics_output/metric_时间戳/
```

如果显式采集 trace，会导出到：

```text
/Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main/trace_output/traces_时间戳.csv
```

## 测试 ID 清单

### 故障修复 Mitigation

```text
k8s_target_port-misconfig-mitigation-1
k8s_target_port-misconfig-mitigation-2
k8s_target_port-misconfig-mitigation-3
auth_miss_mongodb-mitigation-1
revoke_auth_mongodb-mitigation-1
revoke_auth_mongodb-mitigation-2
user_unregistered_mongodb-mitigation-1
user_unregistered_mongodb-mitigation-2
misconfig_app_hotel_res-mitigation-1
scale_pod_zero_social_net-mitigation-1
assign_to_non_existent_node_social_net-mitigation-1
astronomy_shop_kafka_queue_problems-mitigation-1
redeploy_without_PV-mitigation-1
wrong_bin_usage-mitigation-1
```

### 故障定位 Localization

```text
k8s_target_port-misconfig-localization-1
k8s_target_port-misconfig-localization-2
k8s_target_port-misconfig-localization-3
auth_miss_mongodb-localization-1
revoke_auth_mongodb-localization-1
revoke_auth_mongodb-localization-2
user_unregistered_mongodb-localization-1
user_unregistered_mongodb-localization-2
misconfig_app_hotel_res-localization-1
scale_pod_zero_social_net-localization-1
assign_to_non_existent_node_social_net-localization-1
container_kill-localization
pod_failure_hotel_res-localization-1
pod_kill_hotel_res-localization-1
network_loss_hotel_res-localization-1
network_delay_hotel_res-localization-1
astronomy_shop_ad_service_failure-localization-1
astronomy_shop_ad_service_high_cpu-localization-1
astronomy_shop_ad_service_manual_gc-localization-1
astronomy_shop_cart_service_failure-localization-1
astronomy_shop_image_slow_load-localization-1
astronomy_shop_kafka_queue_problems-localization-1
astronomy_shop_loadgenerator_flood_homepage-localization-1
astronomy_shop_payment_service_failure-localization-1
astronomy_shop_payment_service_unreachable-localization-1
astronomy_shop_product_catalog_service_failure-localization-1
astronomy_shop_recommendation_service_cache_failure-localization-1
wrong_bin_usage-localization-1
```

### 根因分析 Analysis

```text
k8s_target_port-misconfig-analysis-1
k8s_target_port-misconfig-analysis-2
k8s_target_port-misconfig-analysis-3
auth_miss_mongodb-analysis-1
revoke_auth_mongodb-analysis-1
revoke_auth_mongodb-analysis-2
user_unregistered_mongodb-analysis-1
user_unregistered_mongodb-analysis-2
misconfig_app_hotel_res-analysis-1
scale_pod_zero_social_net-analysis-1
assign_to_non_existent_node_social_net-analysis-1
redeploy_without_PV-analysis-1
wrong_bin_usage-analysis-1
```

### 异常检测 Detection

```text
k8s_target_port-misconfig-detection-1
k8s_target_port-misconfig-detection-2
k8s_target_port-misconfig-detection-3
auth_miss_mongodb-detection-1
revoke_auth_mongodb-detection-1
revoke_auth_mongodb-detection-2
user_unregistered_mongodb-detection-1
user_unregistered_mongodb-detection-2
misconfig_app_hotel_res-detection-1
scale_pod_zero_social_net-detection-1
assign_to_non_existent_node_social_net-detection-1
container_kill-detection
pod_failure_hotel_res-detection-1
pod_kill_hotel_res-detection-1
network_loss_hotel_res-detection-1
network_delay_hotel_res-detection-1
astronomy_shop_ad_service_failure-detection-1
astronomy_shop_ad_service_high_cpu-detection-1
astronomy_shop_ad_service_manual_gc-detection-1
astronomy_shop_cart_service_failure-detection-1
astronomy_shop_image_slow_load-detection-1
astronomy_shop_kafka_queue_problems-detection-1
astronomy_shop_loadgenerator_flood_homepage-detection-1
astronomy_shop_payment_service_failure-detection-1
astronomy_shop_payment_service_unreachable-detection-1
astronomy_shop_product_catalog_service_failure-detection-1
astronomy_shop_recommendation_service_cache_failure-detection-1
redeploy_without_PV-detection-1
wrong_bin_usage-detection-1
flower_node_stop-detection
flower_model_misconfig-detection
```

也可以从 registry 查询当前代码支持的 problem id：

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main
python - <<'PY'
from aiopslab.orchestrator.problems.registry import ProblemRegistry
for pid in ProblemRegistry().get_problem_ids():
    print(pid)
PY
```

## 演示推荐节奏

第一次演示：

```bash
python scripts/aiopslab_start.py misconfig_app_hotel_res-mitigation-1
```

给 NimbusOps：

```text
当前运行环境存在服务故障，请排查根因并完成修复，使服务恢复正常。不要假设故障类型，先观察系统状态、日志和事件；修复后说明你做了什么。
```

评测：

```bash
python scripts/aiopslab_eval.py
```

下一轮演示同一个故障：

```bash
python scripts/aiopslab_cleanup.py --reset-scenario
```

换一个 scenario：

```bash
python scripts/aiopslab_cleanup.py
python scripts/aiopslab_start.py <another_problem_id>
```

真正重装平台：

```bash
python scripts/aiopslab_cleanup.py --full
```

## 常见问题

### 缺少 config.yml

```bash
cd /Users/xujunjie/Desktop/个人文件/软件杯/Harness-Agent/AIOpsLab-main
cp aiopslab/config.yml.example aiopslab/config.yml
```

并将 `k8s_host` 改为 `kind`。

### aiopslab-applications 为空

```bash
git submodule update --init --recursive
```

### docker info 连接失败

Docker Desktop 或 OrbStack 没有运行，或者 Docker context 指向了已经停止的运行时。
启动容器运行时并选择正确 context。

### kubectl config current-context 为空

kind 集群尚未创建，或 Kubeconfig 没有加载。重新创建 kind 集群后再检查。

### Pod 长时间处于 ImagePullBackOff

先查看：

```bash
kubectl describe pod <pod-name> -n <namespace>
```

通常是镜像下载、代理或网络问题。首次运行需要访问容器镜像仓库以及 OpenEBS
远程安装地址。

### 评测脚本提示没有活动场景

`.aiopslab-terminal/state.json` 不存在。需要先成功运行：

```bash
python scripts/aiopslab_start.py <problem_id>
```

### NimbusOps 看不到测试 namespace

确认 NimbusOps 与当前终端使用同一份 `KUBECONFIG`：

```bash
echo "${KUBECONFIG:-$HOME/.kube/config}"
kubectl config current-context
kubectl auth can-i '*' '*' --all-namespaces
```

mitigation 测试要求 Agent 不仅能读取资源，还要能修改 Deployment、Service、
ConfigMap 等对象。

## 销毁整个测试集群

平时不需要删除 kind 集群。如果要彻底释放本地 Kubernetes 资源：

```bash
kind delete cluster
```

删除集群后，下次测试前需要重新创建集群，并重新准备平台组件。
