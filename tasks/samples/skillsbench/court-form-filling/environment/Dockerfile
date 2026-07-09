# Rebase mode: start from the coder_eval runtime image so the container inherits
# the framework runtime + org.coder-eval.version label, then re-apply the task's
# own setup on top. The upstream skillsbench base is ubuntu:24.04.
FROM coder-eval-agent:latest

WORKDIR /root

# poppler-utils provides `pdftotext`, which the verifier uses to read the filled
# form. The pip libs are the PDF form-filling toolkit plus the verifier's runner.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        pypdf==5.1.0 \
        fillpdf==0.7.3 \
        pdfrw==0.4 \
        PyPDF2==3.0.1 \
        reportlab==4.2.5 \
        cryptography==43.0.3 \
        pytest==8.4.1 \
        pytest-json-ctrf==0.3.5

# The blank SC-100 form, baked at the absolute path the prompt references.
COPY sc100-blank.pdf /root/sc100-blank.pdf
