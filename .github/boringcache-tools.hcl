variable "REBUILD_TOOLS" {
  default = false
}

group "default" {
  targets = ["tools"]
}

target "tools" {
  context = "."
  dockerfile = "docker/tools.Dockerfile"
  platforms = ["linux/amd64"]
  tags = ["lintro-tools:validation"]
  output = ["type=docker"]
  no-cache = REBUILD_TOOLS
}
