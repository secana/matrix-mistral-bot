{
  description = "Development environment for matrix-mistral-bot";

  inputs = { nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable"; };

  outputs = { self, nixpkgs }:
    let
      allSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      forAllSystems = fn:
        nixpkgs.lib.genAttrs allSystems
          (system: fn {
            pkgs = import nixpkgs {
              inherit system;
              config.permittedInsecurePackages = [ "olm-3.2.16" ];
            };
          });

    in
    let
      pythonFiles = "src/bot.py src/chat.py src/search.py src/verification.py src/cross_signing.py";
    in
    {
      devShells = forAllSystems ({ pkgs }: {
        default = pkgs.mkShell {
          buildInputs = with pkgs; [
            uv
            docker
            git
            olm
            cmake
            gcc
          ];

          shellHook = ''
            export LIBRARY_PATH="${pkgs.olm}/lib:$LIBRARY_PATH"
            export C_INCLUDE_PATH="${pkgs.olm}/include:$C_INCLUDE_PATH"
          '';
        };
      });

      apps = forAllSystems ({ pkgs }: {
        lint = {
          type = "app";
          program = toString (pkgs.writeShellScript "lint" ''
            exec ${pkgs.uv}/bin/uvx ruff check ${pythonFiles}
          '');
        };
        format = {
          type = "app";
          program = toString (pkgs.writeShellScript "format" ''
            exec ${pkgs.uv}/bin/uvx ruff format ${pythonFiles}
          '');
        };
        format-check = {
          type = "app";
          program = toString (pkgs.writeShellScript "format-check" ''
            exec ${pkgs.uv}/bin/uvx ruff format --check ${pythonFiles}
          '');
        };
        build = {
          type = "app";
          program = toString (pkgs.writeShellScript "build" ''
            exec ${pkgs.docker}/bin/docker build -t matrix-mistral-bot .
          '');
        };
      });
    };
}
