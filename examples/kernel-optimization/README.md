# Kernel Optimization Pipeline

This pipeline takes one compute operation from a simple first implementation
through compilation, reference validation, benchmarking, and iterative
optimization.

## Flow

```mermaid
flowchart LR
    A[Pick operation] --> B[First version]
    B --> C{Compiles?}
    C -- No --> B
    C -- Yes --> D[Check correctness]
    D --> E{Matches reference?}
    E -- No --> F[Fix bug]
    F --> D
    E -- Yes --> G[Benchmark kernel]
    G --> I{Reached roofline?}
    I -- No --> H[Optimize kernel]
    H --> D
    I -- Yes --> J[Done]

    style A fill:#cfe2f3
    style B fill:#fce5cd
    style C fill:#fce5cd
    style D fill:#d9ead3
    style E fill:#d9ead3
    style F fill:#d9ead3
    style G fill:#fff2cc
    style H fill:#ead1dc
    style I fill:#ead1dc
    style J fill:#d9eaf0
```

SimpleCode executes agents and prompts sequentially, so the conditional loops in
the diagram are implemented by the `kernel-engineer` agent inside its numbered
prompts. Keeping all four prompts in one agent also preserves the runtime session
and its context between iterations.

## Prompts

1. Pick the requested operation and implement a clear first version.
2. Compile and fix build failures until the build passes.
3. Compare with the reference and fix correctness bugs until all checks pass.
4. Benchmark, optimize, revalidate, and repeat until the stated target is reached.

## Usage

Copy and complete the task template, then run the pipeline against the target
repository:

```bash
cp examples/kernel-optimization/TASK.md ~/projects/myapp/KERNEL-TASK.md

simplecode run \
  -f examples/kernel-optimization/pipeline.yaml \
  -r claude \
  -w ~/projects/myapp \
  -i ~/projects/myapp/KERNEL-TASK.md \
  -t 86400
```

The final human gate lets you review the measurements and correctness evidence.
Type `PNS` to approve the result, provide feedback to continue the same agent
session, or type `stop` to reject it.
