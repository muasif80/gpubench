"""Tests for the report template's linter and its YAML subset reader.

Run them from the directory that contains the ``template`` package:

    python -m unittest discover -s template/tests -t .
    python -m unittest template.tests.test_lint -v

Standard library only, by contract: no pytest, no jsonschema, no PyYAML.
"""
