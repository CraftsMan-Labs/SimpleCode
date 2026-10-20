terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = "~> 2.18"
    }
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.6"
    }
  }
}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

variable "workspace_image" {
  type        = string
  description = "Locally built SimpleCode agent image"
  default     = "simplecode-agent:latest"
}

resource "coder_agent" "main" {
  arch          = "amd64"
  os            = "linux"
  dir           = "/workspace/repo"
  api_key_scope = "no_user_data"

  display_apps {
    vscode       = false
    web_terminal = true
    ssh_helper   = true
  }
}

resource "docker_container" "workspace" {
  count    = data.coder_workspace.me.start_count
  image    = var.workspace_image
  name     = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname = data.coder_workspace.me.name
  user     = "1000:1000"

  entrypoint = ["sh", "-c", replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "coder.localhost")]

  env = [
    "CODER_AGENT_TOKEN=${coder_agent.main.token}",
    "ANTHROPIC_BASE_URL=http://9router:20128",
    "OPENAI_BASE_URL=http://9router:20128/v1",
    "OPENAI_API_BASE=http://9router:20128/v1",
  ]

  networks_advanced {
    name = "simplecode-stack"
  }
}
