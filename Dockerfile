FROM python:3.10-slim

# Install ffmpeg and clean up apt cache to keep image size small
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the port
EXPOSE 5000

# Set environment variables for production
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# Command to run the application using gunicorn
CMD ["gunicorn", "app:app", "-w", "4", "-b", "0.0.0.0:5000", "--timeout", "300"]
