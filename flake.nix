{
  description = "CyberGym HUD/Daytona direct Claude Opus 5 operator toolchain";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
      toolchainFor =
        pkgs: with pkgs; [
          bash
          clang
          cmake
          coreutils
          curl
          docker-client
          findutils
          git
          git-lfs
          gnumake
          gnugrep
          gnused
          gnutar
          gzip
          jq
          nodejs_22
          openssh
          pkg-config
          poetry
          python312
          uv
        ];
      mkOperatorApp =
        pkgs: name: command:
        let
          app = pkgs.writeShellApplication {
            name = "cybergym-${name}";
            runtimeInputs = toolchainFor pkgs;
            text = ''
              export UV_PYTHON="${pkgs.python312}/bin/python3.12"
              export UV_PYTHON_DOWNLOADS=never
              root="$(git rev-parse --show-toplevel)"
              target="$root/${command}"
              if [ ! -x "$target" ]; then
                echo "run this command from the CyberGym Anthropic Git checkout" >&2
                exit 2
              fi
              exec "$target" "$@"
            '';
          };
        in
        {
          type = "app";
          program = "${app}/bin/cybergym-${name}";
        };
      mkDispatcherApp =
        pkgs: name: subcommand:
        let
          app = pkgs.writeShellApplication {
            name = "cybergym-${name}";
            runtimeInputs = toolchainFor pkgs;
            text = ''
              export UV_PYTHON="${pkgs.python312}/bin/python3.12"
              export UV_PYTHON_DOWNLOADS=never
              root="$(git rev-parse --show-toplevel)"
              dispatcher="$root/integrations/hud/ops/cybergym-ops"
              if [ ! -x "$dispatcher" ]; then
                echo "run this command from the CyberGym Anthropic Git checkout" >&2
                exit 2
              fi
              exec "$dispatcher" "${subcommand}" "$@"
            '';
          };
        in
        {
          type = "app";
          program = "${app}/bin/cybergym-${name}";
        };
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShellNoCC {
            packages = toolchainFor pkgs;
            env = {
              UV_PYTHON = "${pkgs.python312}/bin/python3.12";
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              echo "CyberGym Anthropic operator shell: ${system}, Python 3.12"
              echo "Direct claude-opus-5 is pinned; task runtimes retain no-public-egress isolation."
            '';
          };
        }
      );

      apps = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = mkDispatcherApp pkgs "preflight" "preflight";
          setup = mkOperatorApp pkgs "setup" "integrations/hud/ops/setup.sh";
          configure = mkOperatorApp pkgs "configure" "integrations/hud/ops/configure-secrets.sh";
          preflight = mkDispatcherApp pkgs "preflight" "preflight";
          smoke = mkDispatcherApp pkgs "smoke" "smoke";
          campaign-preflight = mkDispatcherApp pkgs "campaign-preflight" "campaign-preflight";
          campaign = mkDispatcherApp pkgs "campaign" "campaign";
          daytona-preflight = mkDispatcherApp pkgs "daytona-preflight" "daytona-preflight";
          daytona-campaign = mkDispatcherApp pkgs "daytona-campaign" "daytona-campaign";
          reconcile = mkOperatorApp pkgs "reconcile" "integrations/hud/ops/reconcile.sh";
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          toolchain = pkgs.runCommand "cybergym-anthropic-operator-toolchain" {
            nativeBuildInputs = toolchainFor pkgs;
          } ''
            test "$(python3.12 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = 3.12
            uv --version
            poetry --version
            docker --version
            node --version
            git lfs version
            touch "$out"
          '';
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixfmt-rfc-style);
    };
}
