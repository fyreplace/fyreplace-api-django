.PHONY: build protobufs protos static migrations emails

build: protobufs emails

protobufs: protos protos/__init__.py

protos:
	uv run python -m grpc_tools.protoc \
		--proto_path=. \
		--python_out=. \
		--grpc_python_out=. \
		--pyi_out=. \
		protos/*.proto

protos/__init__.py:
	touch $@

emails:
	yes | npx mjml users/templates/*.html.mjml -c.minify=true -o users/templates

static:
	python manage.py collectstatic --no-input

migrations:
	python manage.py migrate
