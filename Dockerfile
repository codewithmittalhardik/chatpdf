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

# Create a non-root user and grant ownership of /code directory
RUN useradd -m -u 1000 user && chown -R user:user /code
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Hugging Face Spaces expects your app to run on port 7860
EXPOSE 7860

# Start command
# -b 0.0.0.0:7860 binds the server to all interfaces on port 7860
# Increase timeout to 300 seconds (5 minutes) to handle large PDFs
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn -b 0.0.0.0:7860 --timeout 300 chatpdf_project.wsgi:application"]