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
          caddy
          clang
          cmake
          coreutils
          curl
          docker-client
          findutils
          gcc
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
          p7zip
          pkg-config
          poetry
          python312
          tmux
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
              export XDG_CACHE_HOME="''${CYBERGYM_OPERATOR_CACHE:-/srv/cybergym/operator-cache}"
              export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
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
              export XDG_CACHE_HOME="''${CYBERGYM_OPERATOR_CACHE:-/srv/cybergym/operator-cache}"
              export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
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
              LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
              XDG_CACHE_HOME = "/srv/cybergym/operator-cache";
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
          bootstrap = mkOperatorApp pkgs "bootstrap" "integrations/hud/ops/bootstrap-host.sh";
          bootstrap-session = mkOperatorApp pkgs "bootstrap-session" "integrations/hud/ops/bootstrap-session.sh";
          setup = mkOperatorApp pkgs "setup" "integrations/hud/ops/setup.sh";
          configure = mkOperatorApp pkgs "configure" "integrations/hud/ops/configure-secrets.sh";
          update-keys = mkOperatorApp pkgs "update-keys" "integrations/hud/ops/update-secrets.sh";
          preflight = mkDispatcherApp pkgs "preflight" "preflight";
          smoke = mkDispatcherApp pkgs "smoke" "smoke";
          campaign-preflight = mkDispatcherApp pkgs "campaign-preflight" "campaign-preflight";
          campaign = mkDispatcherApp pkgs "campaign" "campaign";
          daytona-preflight = mkDispatcherApp pkgs "daytona-preflight" "daytona-preflight";
          daytona-ready = mkDispatcherApp pkgs "daytona-ready" "daytona-ready";
          daytona-control = mkDispatcherApp pkgs "daytona-control" "daytona-control";
          daytona-campaign = mkDispatcherApp pkgs "daytona-campaign" "daytona-campaign";
          daytona-plan = mkOperatorApp pkgs "daytona-plan" "integrations/hud/ops/plan-daytona-lanes.py";
          daytona-lane = mkDispatcherApp pkgs "daytona-lane" "daytona-lane";
          daytona = mkOperatorApp pkgs "daytona" "integrations/hud/ops/daytona-fleet.sh";
          run-missing-pass1 = mkDispatcherApp pkgs "run-missing-pass1" "daytona-missing-pass1";
          continue-pass3 = mkDispatcherApp pkgs "continue-pass3" "daytona-continue-pass3";
          finalize-pass3 = mkDispatcherApp pkgs "finalize-pass3" "daytona-finalize";
          round-barrier = mkDispatcherApp pkgs "round-barrier" "daytona-round-barrier";
          install-corpus = mkOperatorApp pkgs "install-corpus" "integrations/hud/ops/install-corpus.py";
          install-relay = mkOperatorApp pkgs "install-relay" "integrations/hud/ops/install-relay.sh";
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
