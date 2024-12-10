# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /main

# Copy the requirements file to the working directory
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code to the working directory
COPY . .

# Expose the port that FastAPI will run on
EXPOSE 9009

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9009"]
