import os
from glob import glob
from importlib import resources
from os.path import dirname, join

from django.core.management.base import BaseCommand
from grpc_tools.protoc import main as run_protoc
from tqdm import tqdm


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        self.generate_protos(progress=True)

    def generate_protos(self, progress: bool = False):
        management_dir = dirname(dirname(__file__))
        project_dir = dirname(dirname(management_dir))
        os.chdir(project_dir)
        files = glob(join("protos", "*.proto"))

        for file in tqdm(files) if progress else files:
            self.generate_proto(file)

    def generate_proto(self, file: str):
        default_protos_dir = (resources.files("grpc_tools") / "_proto").resolve()
        run_protoc(
            [
                __file__,
                f"--proto_path={default_protos_dir}",
                "--proto_path=.",
                "--python_out=.",
                "--grpc_python_out=.",
                "--pyi_out=.",
                file,
            ]
        )
