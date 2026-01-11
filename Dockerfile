# Use Python 3.12 (Matches your local environment)
FROM python:3.12

# Set the working directory inside the container
WORKDIR /code

# Copy requirements first (to cache dependencies and build faster)
COPY ./requirements.txt /code/requirements.txt

# Install dependencies
# --no-cache-dir keeps the image small
# --upgrade ensures you get the latest compatible versions
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copy the rest of your application code
COPY . .

# Create a non-root user (Mandatory for Hugging Face Security)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Hugging Face Spaces expects your app to run on port 7860
EXPOSE 7860

# Start command
# -b 0.0.0.0:7860 binds the server to all interfaces on port 7860
CMD ["gunicorn", "-b", "0.0.0.0:7860", "app:app"]