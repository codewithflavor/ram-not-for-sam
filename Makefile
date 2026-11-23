.PHONY: all test clean
all:
	@echo "Run ./ram-not-for-sam --help (Python 3 required)"

test:
	python3 -m unittest -v

clean:
	@:
