# Third-Party Notices

NimbusOps includes adapted or redistributed open-source components. Their
copyright notices and license terms are retained below or in the corresponding
component directory.

## nanobot — Agent Runtime

- **Source**: https://github.com/HKUDS/nanobot
- **Use in NimbusOps**: The Agent Runtime design and selected implementation
  elements in `nimbusops-agent` were adapted for the NimbusOps product runtime.
- **Copyright**: Copyright (c) 2025-present Xubin Ren and the nanobot contributors
- **License**: MIT License

```text
MIT License

Copyright (c) 2025-present Xubin Ren and the nanobot contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## AIOpsLab — Kubernetes Evaluation Baseline

- **Source**: https://github.com/microsoft/AIOpsLab
- **Use in NimbusOps**: Adapted Kubernetes fault scenarios, orchestration and
  evaluation workflow under `K8S Bench`.
- **Copyright**: Copyright (c) Microsoft Corporation.
- **License**: MIT License

The complete upstream MIT license and required notices are retained at
[`K8S Bench/LICENSE.txt`](./K8S%20Bench/LICENSE.txt) and
[`K8S Bench/NOTICE.txt`](./K8S%20Bench/NOTICE.txt). Notices for bundled
application dependencies remain in their original locations under `K8S Bench`.

## forge-guardrails — Action Guardrails

- **Source**: https://github.com/antoinezambelli/forge
- **Use in NimbusOps**: Rescue parsing, retry budgeting and prerequisite
  tracking in `nimbusops-agent/my_agent/guardrails/action.py`.
- **Copyright**: Copyright (c) 2025-2026 Antoine Zambelli
- **License**: MIT License

The complete license text is retained at
[`nimbusops-agent/my_agent/guardrails/THIRD_PARTY_LICENSE_FORGE`](./nimbusops-agent/my_agent/guardrails/THIRD_PARTY_LICENSE_FORGE).
